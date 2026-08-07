# CI & review convergence

The canonical reference for taking a feature branch to **verified green and review clean**, without
the human doing the shuttling. Orchestrating skills point here; a coding session runs this loop by
default on every feature.

Governing idea: **the orchestrator owns the loop, workers own the fixes.** Work is built on a
branch in the container, and from the first commit the conducting thread is responsible for driving
it to green — running the gate, triaging failures, delegating fixes, and re-entering the loop —
until the change is verified and the review has nothing blocking left. The human is not the message
bus between the gate and the fixer.

Two failure modes this exists to prevent: a broken branch sitting because everyone assumed someone
else was watching, and a loop that spins forever on a failure it can't fix. Both are handled by
the same discipline — watchdogs on everything, and a bounded number of rounds before escalation.

## The draft/ready contract

The firm's repos run CI **only on pull requests that are ready for review**. That makes the PR's own
state the boundary between the session's workspace and the human's:

| PR state | Whose it is | What verifies the code | What the state means |
| --- | --- | --- | --- |
| **Draft** | the session's | the **local gate** — the pipeline's own checks, run in the container | "not for you yet"; no CI runs, no CI minutes burned |
| **Ready** | the human's | **CI on GitHub**, on top of the local gate | "this is for you"; CI is running and should be green |

So the loop has two halves, not one. The session converges the branch **locally** while the PR is a
draft — build, gate, review, fix, repeat — and flips to ready only when the branch is green locally
*and* the review is clean. **Flipping is what starts CI**, and it is also the machine-readable
hand-off signal, so it happens once, deliberately, when the session has real reason to believe the
run will pass.

**Verify the repo's actual trigger; don't assume.** Read `.github/workflows/*`. A PR workflow gated
this way carries `ready_for_review` in its `pull_request` `types:` and a draft guard on the job
(`if: github.event.pull_request.draft == false`, or the `github.event_name != 'pull_request' || …`
form when the workflow also runs on `push`). If the repo's workflows instead run on every PR push,
CI covers the draft phase too — keep the local gate anyway (it's cheaper and faster than a CI round
trip) and treat each push's run as an extra check. A push to a draft that produces **no run at all**
in a gated repo is the expected behavior, not a CI outage.

## The loop

```
build on branch (worker, container)          ── PR is a DRAFT: CI does not run
   └─ local gate: the pipeline's own checks, run in the container
        ├─ red ──► triage ──► fix worker ──► (local gate again)
        └─ green
             └─ code review
                  ├─ blocking findings ──► fix worker ──► (local gate again, then re-review)
                  └─ clean + locally green
                       └─ FLIP the PR to READY          ── this is what starts CI
                            ├─ notify the human now (don't make them wait on the run)
                            └─ CI watchdog stays armed on the post-flip run
                                 ├─ green ──► merge-ready; the human merges
                                 └─ red ──► triage FIRST, then:
                                      ├─ ours ──► back to DRAFT, fix, re-gate, flip again
                                      ├─ Actions/provider down ──► confirm with the user,
                                      │     back to DRAFT, converge fully, flip once when ready
                                      └─ other not-ours ──► stay READY, notify with the diagnosis
```

Rules that make it work:

- **The local gate is the real gate.** While the PR is a draft, nothing else is checking the branch.
  Run the project's own lint, type-check, tests, build, and security checks in the container, and
  don't advance on "it should be fine."
- **Green before review.** Don't spend a review pass on code the gate has already rejected — the
  review's findings would be interleaved with build breakage and half of it may be rewritten anyway.
  Fix to green first, then review.
- **Review comments land on the PR.** The final review posts per-finding comments with file:line and
  severity, plus one summary verdict comment, so the record is on the PR rather than in a thread the
  human can't see.
- **The orchestrator picks the review back up.** It reads the posted findings, decides what's
  blocking, and delegates those to a fix worker — it does not hand the human a list of comments to
  route.
- **Any fix re-enters at the gate.** A fix means the local gate runs again and the affected review
  points get re-checked. Never mark converged on the strength of a pre-fix green.
- **Flip once, and mean it.** Ready-for-review says two things at once: CI, start; human, look. Both
  are wasted if the branch wasn't actually converged first.
- **Bound every loop.** ~3 gate-fix rounds on the same failure, ~2 review-fix rounds without
  convergence, then stop and escalate with the diagnosis. A loop that isn't converging is signal.
- **Log what the loop decided.** Fixes made without the human — especially anything that changed
  behavior rather than just satisfying a check — go in the decision log the human reads at sign-off.

## The local gate — reconstruct the pipeline and run it

The gate that runs while the PR is a draft is the pipeline's own gate, executed in the container.
This is the same mechanism the loop falls back on when CI is down or absent; in a draft-gated repo
it is simply the **normal** path for everything before the flip.

- **Reconstruct it from the pipeline definition — job by job, step by step.** Read the workflow
  file(s) and run the same commands the pipeline would, in the same order: install, lint,
  type-check, unit/integration tests, build, and any security or workflow linting the project runs
  (including `actionlint` on changed workflow files and `shellcheck` on changed shell scripts). If
  there's no pipeline to read, use the project's own documented commands (`CLAUDE.md`,
  `package.json` scripts, `Makefile`, `justfile`, `pyproject.toml`).
- **Never substitute the repo's convenience target for the workflow.** `make check`, `npm run
  verify` and their kin drift from CI, and the steps they omit are disproportionately the ones that
  catch real breakage — a formatter check, an asset-manifest guard, a schema-drift check. Diff the
  convenience target against the workflow's steps; **if they disagree, that is a finding worth
  filing**, and the workflow wins in the meantime. An observed case: `make check` was documented as
  "exactly what CI gates on" while omitting the one guard that would have caught that PR's most
  likely failure.
- **Know what it can't cover, and say so.** Some jobs genuinely can't run in the container — a
  matrix across runners, a Docker build with no daemon available, a job needing a repo secret or a
  service the container can't reach, a scanner without network access. Those are the residual risk
  the post-flip CI run actually tests. Report them as **unverified**, in their own line, never
  folded into a table of green rows (`${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md`).
- **Name the environment gap when there is one.** A suite run against a different database version,
  runtime, or OS than production pins is evidence about *this* environment. Say which, so the
  difference is the reader's to weigh.
- **Run it in a worker, not the conductor.** Same dispatch-plus-watchdog discipline; the gate is
  just another worker whose output is a pass/fail plus failing excerpts.
- **Keep the same rounds and bounds.** Local red → triage → fix worker → re-run locally → repeat,
  bounded the same way. Review still runs after local green, and its fixes still re-enter at the
  gate.
- **Be explicit about what verified the change.** The PR and the sign-off package say the local gate
  ran and which checks it ran — a human must never read "green" and assume CI produced it before the
  flip.
- **If the repo has no CI at all**, this loop is the whole story: converge locally, flip to ready
  anyway (it's still the hand-off signal), and say plainly on the PR that no pipeline exists. Surface
  the missing pipeline as a real gap rather than silently routing around it forever.

## The flip, and the run that follows

Flipping the PR from draft to ready is the session's hand-off, and in a gated repo it is also the
event that triggers CI. Both halves matter:

1. **Preconditions.** Local gate green on the PR head, final review clean on that same commit,
   acceptance criteria met, decision log finalized. Not before.
2. **Flip, then notify immediately.** Don't hold the sign-off package hostage to the CI run — the
   human's review of the diff and the decision log can start while the pipeline runs. Say in the
   notification that CI has just started, what it will run, and that merge should wait for green.
3. **Keep the CI watchdog armed.** A CI run is **not** harness-tracked work — no completion
   notification is coming for it. Register a wake-up sized to the pipeline's typical duration, check
   the run non-blockingly, and re-arm with backoff while it's still running. The session is not done
   at the flip; it is done when that run is green (or triaged and escalated).
4. **A green first run is the expectation.** The local gate exists so the post-flip run confirms
   rather than discovers. A red one means the local gate missed something — fix the branch *and*
   note what the gate should have caught, so the next feature's gate covers it.

### Post-flip red: triage before touching the PR's state

**Never flip back to draft on a red run you haven't diagnosed.** The state change is a signal to the
human, and flipping it on someone else's failure sends the wrong one. Classify first:

- **Ours** — a test, lint, type, or build failure caused by the diff; a flake this change
  introduced. → **Flip back to draft**, say why in a PR comment (the failing job and the diagnosis),
  fix it through the normal loop, re-run the local gate, and flip to ready again with a fresh
  notification. Draft is where fixing happens; leaving a known-broken PR sitting in the human's
  queue is the thing this contract exists to prevent.
- **Not ours** — GitHub Actions minutes or concurrency limits, a runner or provider outage, a rate
  limit, an expired or missing repo secret, a pre-existing flake, or a failure that reproduces
  identically on the base branch. → **Leave the PR ready** and **notify the user** with the
  diagnosis: which job, why it isn't the code, and what would clear it (their action — topping up
  minutes, rotating a credential — or simply a re-run once the provider recovers). Flipping back to
  draft here would hide a mergeable PR behind a problem the code didn't cause and the session can't
  fix.
- **Unclear** — reproduce it against the local gate first. If the failure reproduces in the
  container it's ours; if the branch is clean locally and the failure is environmental, treat it as
  not-ours and say what you checked.

Either way it goes on the PR once, not silently. And either way the round budget applies: ~3 rounds
on the same failure, then escalate with the diagnosis rather than flipping back and forth.

## Watchdogs on both halves

Every stage of this loop can go silent, and each needs its own backstop
(`${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md` for the pattern):

- **Worker watchdog.** Every fix/build/review/gate subagent is dispatched in the background with a
  watchdog sized to its expected duration. On fire: progressing → re-arm; silent → stop it, then
  re-dispatch; already done → proceed. Cap re-dispatches (~2) before escalating.
- **CI watchdog.** Armed at the flip (and on any push while the PR is ready), sized to the
  pipeline's typical duration — use the repo's recent run times. Never busy-poll a run that takes
  minutes, and never block the conductor on one.
- **Queue stalls count as stalls.** A run that stays queued well past its normal window is a
  CI-availability problem — treat it as not-ours under the triage above, not as something to keep
  waiting on. In a gated repo, remember that **no run at all while the PR is a draft is correct**;
  it is only a stall if the PR is ready.

## Triage: whose failure is it?

Before delegating a fix — local gate or CI — classify the red. Getting this wrong burns rounds.

**Suspect the provider first when the run failed too fast or waited too long.** Jobs that fail
within seconds of starting never ran anything, and a red check from a job that never ran carries no
information about the code. Treat either of these as the trigger to run the check below, before any
fix worker is dispatched:

- the run failed **almost immediately** (seconds, not minutes), or
- the run sat **far past its normal queue window** and then failed.

Both are the same underlying condition — no runner was ever assigned — surfacing differently
depending on how long the job sat first. Do not use duration alone as the test: the identical outage
has produced 2-second failures on one branch and 67–88-minute queued failures on another, and a
threshold-only rule misclassified the slow one as a real failure.

### Confirming an Actions/provider failure

Gather the evidence, then decide. Any of these on their own is suggestive; together they are
conclusive:

- **No runner was assigned** — the job reports `runner_id: 0` and an empty `runner_name`.
- **No output exists** — the check run's `output.title`/`summary`/`text` are empty, and fetching the
  job's logs returns 404. A job that produced no logs produced no result.
- **Unrelated jobs failed identically.** Three jobs with nothing in common failing in the same
  second is infrastructure, not three simultaneous bugs.
- **The failing job has none of this diff's files in scope.** A frontend job failing on a
  Python-only diff cannot be attributable to it — confirm with
  `git diff --name-only <base>...HEAD` against that job's paths.
- **It reproduces on the base branch.** Recent runs on the base failing the same way — especially
  the exact commit this branch is based on — means it predates the change.
- **The account or org is out of Actions minutes**, or Actions is disabled by policy. Blocked jobs
  are reported as **failed**, not as never-started, which is what makes this look like a code
  failure.

**State the evidence, not the conclusion.** Write down what was checked and what it showed, so a
wrong call is visibly wrong and can be corrected against the record. Triage of this kind has been
wrong before and needed a public correction; the evidence list is what makes that cheap.

### The Actions-failure protocol

Once the evidence points at the provider rather than the diff:

1. **Ask the user to confirm**, presenting the evidence gathered above and the diagnosis drawn from
   it. This is the one CI condition worth a question, because the wrong call sends a fix worker
   after a bug that does not exist — or leaves a real failure unfixed.
2. **Do not block on the answer.** Return the PR to **draft** immediately and keep converging
   locally; that work is productive under either answer. The user's reply only decides where it ends
   up.
3. **Draft is where it stays** until the local gate is green *and* the review is clean — the full
   normal bar. A dead pipeline is not a reason to lower it; it is a reason to stop using the flip as
   a probe.
4. **Flip when the work is genuinely ready**, once and deliberately, and say plainly on the PR that
   the gate was local, exactly which checks ran, and which jobs have no local substitute and are
   therefore unverified (`${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md`). If the flip's run
   fails the same way, that is the known outage — record it once and leave the PR ready; do not
   re-enter the loop.
5. **If the user says it is not an Actions failure**, treat the red as ours and run the normal
   fix loop from draft.
6. **If no answer arrives**, default to this protocol. Staying in draft and continuing to converge
   is the non-destructive choice — it never hands the human a broken PR and never chases a
   phantom bug.

This is deliberately different from other not-ours failures (a pre-existing flake, an expired
secret, a failure reproducing on the base branch of an otherwise-healthy pipeline), where the PR
**stays ready** and the user just gets the diagnosis. The distinction is whether the pipeline can
still produce a verdict at all: if it can, a ready PR with one explained red check is honest; if it
cannot, ready would be claiming a verification that no longer exists.

- **Ours, deterministic** (test failure, lint/type error, build break caused by the diff) → fix
  worker, with the failing job's log excerpt and the command to reproduce locally in the brief.
- **Ours, flaky** (passes on re-run, timing/order-dependent) → don't paper over it with a re-run
  alone; a flaky test the change introduced is a real finding. A pre-existing flake gets one
  re-run and a note.
- **Not ours** (fails identically on the base branch, an upstream outage, an Actions usage or
  concurrency limit, a rate limit, an expired credential) → say so once on the PR, don't burn rounds
  fixing it, and don't flip a ready PR back to draft for it. Notify the user and re-check when the
  base or the provider recovers.
- **Infrastructure/CI itself down** (no runner, workflow can't start, the provider is unavailable)
  → confirm it against the evidence list above, ask the user, and run the Actions-failure protocol:
  back to draft, converge fully locally, flip once when genuinely ready. Not a fix loop, and not a
  ready PR sitting on a red check the pipeline can never clear.

## Anti-patterns

- **Flipping to ready to find out whether it works.** Ready-for-review is a claim, not a probe. Run
  the gate locally first; a red first run costs the human a false alarm.
- **Flipping back to draft on an undiagnosed red** — especially on an Actions limit or an outage,
  which hides a mergeable PR behind a failure the code didn't cause.
- **Leaving a known-broken PR ready** because flipping back felt like an admission. If the failure
  is ours, draft is where the fix belongs.
- **Push-and-forget.** Leaving a broken branch because a worker's completion signal arrived and
  nobody ran the gate.
- **Handing failures to the human before triage.** "CI is failing, what do you want to do?" is an
  escalation only after triage says it's not ours or the round budget is spent.
- **Reviewing a red branch** and then re-reviewing the rewrite — two passes for one review.
- **Re-running until green.** Retrying a failing job without a diagnosis hides real flakiness and
  burns rounds.
- **Dispatching a fix worker at a job that never ran.** A red check from a job with no runner, no
  logs and no output is not a failing test — chasing it invents a bug and burns a round finding
  nothing.
- **Using the flip as a probe when the pipeline is dead.** If CI cannot produce a verdict, flipping
  to ready claims a verification that doesn't exist. Converge in draft and flip once, when done.
- **Blocking on CI.** Sitting in a foreground wait for a run instead of a watchdog-backed check.
- **Declaring converged from a stale green.** A green from before the last fix proves nothing.
- **Treating "no CI run on my draft" as an outage.** In a gated repo that's the design.
- **Ending the session at the flip.** The watchdog stays armed until the post-flip run resolves.

## See also

- `${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md` — the dispatch → watchdog → stop-and-re-dispatch
  pattern every worker and the CI watch use.
- `${CLAUDE_PLUGIN_ROOT}/shared/parallelization.md` — running independent fixes concurrently.
- `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md` — the bar this loop converges on.

`${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md` — how the gate's results are proven and
reported, including what "unverified" must never be folded into.
- `${CLAUDE_PLUGIN_ROOT}/references/devops/cicd.md` — how the gated pipeline itself is wired.
