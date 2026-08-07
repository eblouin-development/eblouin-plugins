# Verification evidence

The canonical reference for proving a change does what it claims. Any skill that fixes a bug, writes
a regression test, reviews a fix, or signs off on a branch points here.

Governing idea: **the presence of code is not evidence of its effect.** A diff that reads correctly,
a test that passes, and a green gate are three things that can all be true while the change does
nothing at all. The only evidence that a fix works is having *watched it not work* — the failure
reproduced first, then closed by the change and nothing else.

This doctrine exists because the opposite keeps happening. A guard set as a class attribute that the
framework's `__init__` silently overwrites, so a six-file diff changes no behavior and the gate stays
green. A regression test asserting a substring that appears somewhere else on the page, so it cannot
fail. A verification harness that no-ops, so both arms of the comparison run the same code. Each of
these looked like a fix, passed review by reading, and was inert.

## The reproduction gate

Before a fix is called done, run the change in both directions and record what happened:

1. **Revert only the source**, leaving the new tests in place:
   `git checkout <base> -- <the source files the fix touched>`.
2. **Run the test.** It must fail — and fail with the message the bug predicts, not some unrelated
   error. A test that fails because of an import error or a missing fixture has proven nothing.
3. **Restore the fix** (`git checkout HEAD -- <same paths>`) and run again. It must pass.
4. **Confirm the tree is clean** (`git status`) so the experiment left nothing behind.
5. **Record both results** — the actual failure text and the pass — wherever the fix is reported.

> **Never use `git stash push <paths>` for step 1.** Against an already-committed tree it silently
> stashes nothing and exits 0, so both runs execute the *fixed* code and the comparison reports a
> false pass. This has produced a real false negative on a real bug. `git checkout <base> -- <paths>`
> is the mechanism; stash is not a substitute.

Where reverting the source isn't practical — a fix that is one line inside a large file, a template,
a CSS rule — the equivalent is to **remove the specific thing the test is pinning** (strip the
attribute, delete the rule), run the single test, watch it fail, and restore. The unit of the
experiment is the behavior, not the file.

## Can this test fail?

A test that cannot fail is worse than no test: it occupies the slot where a real guard would go and
it reports green forever. Before accepting a new test, establish what would make it red.

Failure modes seen repeatedly, in rough order of how often they slip through:

- **Asserting too wide a target.** Checking that a whole page or whole file contains
  `data-confirm=` when three other elements also carry it. The assertion passes no matter what
  happens to the element under test. Assert the *specific* interpolated value belonging to this
  case.
- **Proving a weaker property than the one that matters.** Value equality over a handful of literal
  dicts when the code's real contract is byte/type equivalence; asserting the observable side effect
  (a toast fired) when the claim is about the underlying state (the audit row is correct).
- **A subject that never executes in the environment that gates it.** A browser suite that installs
  its package but not its browser and therefore *skips* in CI; a job whose steps live only in a
  workflow file nobody has copied into place. Green, and gating nothing.
- **Vacuous branches.** A test parameterized over two intentional divergences that only ever
  exercises one; an assertion downstream of a guard that always short-circuits.
- **A harness that no-ops.** The stash case above, and its relatives: a patch that targets a symbol
  the code doesn't import, a fixture that silently falls back to the real object, an env var read
  before the test sets it.

The check is mechanical: **make the code wrong on purpose and confirm the test notices.** If you
can't make it fail, you haven't written a test — you've written an assertion about something else.

## Reporting evidence

When a fix, a gate run, or a review verdict is written up, state what was actually executed and what
it produced. The distinction that matters to a reader is *verified* versus *assumed*, and only the
report can carry it.

- **Name the command and the result**, not the conclusion. "6,898 passed, 12 skipped" beats "tests
  pass." Paste the failure text from the reproduction gate.
- **Separate what ran from what didn't.** A job with no local substitute — a Docker build, a runner
  matrix, anything needing a secret or a service the container can't reach — is **unverified, not
  green**, and it says so in its own line. Never let a table of green rows imply coverage of a row
  that isn't there.
- **State residual caveats plainly** rather than burying them: a suite run on a different database
  version than production pins, a pre-existing warning count, a gate step that was skipped.
- **Correct the record in place.** A verification claim that turns out wrong gets an explicit
  correction where it was made, not a quiet revision — the earlier claim has already been relied on.

## Verifying someone else's fix

Re-verification is a distinct pass from reading the diff, and it starts from the assumption that the
fix may be inert.

- **Check every finding, not a sample.** Findings resurface after being reported fixed more often
  than any other review outcome. A fix round is verified when each finding has its own result.
- **Re-derive the numbers rather than accepting them.** Recompute the contrast ratio, re-run the
  query plan, re-read the specificity. A stated figure that nobody recomputed is an assumption
  wearing a number.
- **Distinguish "the code is present" from "the behavior changed."** Presence is what a diff shows;
  behavior is what the reproduction gate shows. On any branch that has already produced one inert
  fix, presence stops counting as evidence entirely.
- **Watch for the fix that lands on some of its sites.** A ruling applied to one of three partials,
  one of four call sites, one of two bulk paths, is a partially-applied fix and the finding is still
  open (`${CLAUDE_PLUGIN_ROOT}/references/review/review-dimensions.md`).

## See also

`${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md` — the bar this evidence is produced against.

`${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md` — the local gate that runs this evidence, and the
triage that decides whether a red signal is even about the code.

`${CLAUDE_PLUGIN_ROOT}/references/review/review-dimensions.md` — the finding classes a review looks
for, including the ones this doctrine is the countermeasure to.
