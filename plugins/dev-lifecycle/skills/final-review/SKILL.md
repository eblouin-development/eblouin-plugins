---
name: "final-review"
description: "The last-mile review the live agent runs ITSELF — never delegated to a subagent — on a finished PR before flipping it ready for the human: production readiness, security, comment relevance (no changelog-style comments in code), information placed where it belongs (PR/issue, not comments), and the FULL test suite run on the final head. Use this skill WHENEVER a feature is fully built and needs its final pass before hand-off — it is the mandatory final stage of a `coding-session` (the conductor invokes it inline), and it triggers on direct asks like \"final review this PR\", \"is this production ready\", \"check this before I open it for review\", \"pre-flip review\". It loads the lifecycle skills for every layer the diff touches (frontend, backend, mobile, devops, testing, documentation, …) and reviews against their standards, sweeps every comment in the diff against the code-comments doctrine, and runs the entire local gate — full suite, lint, types, build — before the PR opens. Distinct from `code-review`, which reviews one diff or one step (and stays the per-step subagent stage); this is the whole-PR pre-flip gate. It never merges — the human merges."
---

# Final review

The last set of eyes before a PR is handed to a human — run by the **live agent itself**, in its own
thread, never dispatched to a subagent. Per-step reviews are delegated because their context is
disposable; the final review is inlined because its context is the whole point: the agent that
conducted the build holds the plan, the decision log, every per-step verdict, and every judgment
call — exactly what's needed to spot the step-3 comment that step 7 made false, the decision that
belongs in the PR body instead of a code comment, and the acceptance criterion no single step owned.

Its charter: the code is **production-ready and secure**, every comment in the diff describes the
**current** code (nothing reads like a changelog), information lives **where it belongs** — the PR
and the issue carry the story of the change, the code carries only its present — and the **entire
test suite** has run green on the final head. Only then does the PR open for the human.

## Core rules

- **Run it yourself.** This skill is the standing exception to "orchestrate, don't inline": the
  final review runs in the conducting thread. Delegating it to a fresh subagent throws away the
  session context that makes the final pass worth having.
- **Load the canon for every layer the diff touches, first.** Before judging anything, invoke (via
  the Skill tool) the lifecycle skill for each surface in the diff — `dev-lifecycle:frontend`,
  `dev-lifecycle:backend`, `dev-lifecycle:mobile`, `dev-lifecycle:devops`,
  `dev-lifecycle:testing`, `dev-lifecycle:data`, `dev-lifecycle:documentation` as applicable — plus
  the doctrines: `${CLAUDE_PLUGIN_ROOT}/shared/code-comments.md`,
  `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`,
  `${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md`,
  `${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md`, and the repo's own `CLAUDE.md`. Each skill's
  standards (accessibility, contract discipline, migration hygiene, doc upkeep…) are the review
  criteria for its layer — that is how documentation, comments, and layer expectations all get a
  final check under the skill that owns them.
- **The whole suite, not a sample.** Per-step gates run what the step touched; the final gate runs
  **everything** — the full test suite, lint, type-check, build, and the rest of the reconstructed
  local gate — on the final head. A PR opens for the human only on a fully green head.
- **Comments describe the present; the PR describes the change.** Sweep every comment in the diff
  against `${CLAUDE_PLUGIN_ROOT}/shared/code-comments.md`. A comment that reads like a changelog
  entry *is* a changelog entry in the wrong place: move its substance to the PR description (or
  decision log, or the issue) and delete it from the code. Fix stale comments; add missing doc
  comments.
- **Fix small things directly; route big things.** Comment cleanup, doc-comment gaps, moving
  information into the PR body — do these yourself in this pass. Substantive code findings go
  through the normal fix loop (build skills / fix workers), and the gate and the affected review
  passes re-run on the new head. Never declare convergence on a green from before the last fix.
- **Never merge.** The output is a PR flipped to ready (pipeline) or a verdict (standalone). Merge
  is the human's.

## Workflow

### 1. Scope the whole PR
Establish the base and the full diff (`git diff <base>...HEAD`), list the changed files, and group
them by layer (frontend / backend / mobile / infra / tests / docs). Pull up the plan or issue's
acceptance criteria and the PR's decision log — they are review inputs, not decoration.

### 2. Load the canon
Invoke the lifecycle skill for each layer present in the diff, plus the doctrines and the repo's
`CLAUDE.md` (core rule above). State what you loaded — the same verifiability rule subagents are
held to applies to the conductor.

### 3. Production-readiness and security pass
Review the full diff against each loaded skill's standards, weighted toward what ships:

- **Production readiness** — configuration and secrets handled per the deploy surface (new env vars
  documented *and* passed through); migrations present, ordered, and reversible where the project
  expects it; error handling that fails safely with non-leaky messages; no debug leftovers,
  commented-out code, or dead flags; logging/observability consistent with the project; anything
  `devops`/`infrastructure` standards require for the change to actually run where it's deployed.
- **Security** — every touched path against `${CLAUDE_PLUGIN_ROOT}/references/security/owasp.md`
  and `${CLAUDE_PLUGIN_ROOT}/references/security/secure-baseline.md`: authn/authz on protected
  routes, no injection, no secrets in code or logs, no insecure default the baseline forbids.
- **Integration findings** — what per-step review structurally cannot see: cross-step
  contradictions, an earlier ruling applied to only some of its sites, the acceptance criteria end
  to end, packaging (a file the image never copies, a build-time exclusion), interaction with other
  in-flight PRs. The finding classes are
  `${CLAUDE_PLUGIN_ROOT}/references/review/review-dimensions.md`; don't re-litigate per-step
  findings already settled.

### 4. Comment and information-placement sweep
Read **every comment in the diff** (and any comment adjacent to changed code) and classify it:

- **Changelog / change narration** ("previously…", "changed to…", "now uses…", "removed the old…",
  "per review") → if the information matters, relocate it — a decision goes to the PR's decision
  log, context for the change to the PR description, follow-up work to the issue — then delete the
  comment. If it carries nothing the PR/issue doesn't already say, just delete it.
- **Stale** — describes behavior the build changed → rewrite to match the current code, or delete.
- **Missing** — a new or changed function/method/class with no doc comment, or genuinely unusual
  code with no why → add it (purpose, usage, data shapes; present-tense why).
- **Fine** — describes the present accurately → leave it alone.

These fixes are the live agent's own edits, committed on the feature branch. This sweep is also the
last check that the **PR body and issue are complete**: the summary matches what actually shipped,
the decision log holds every judgment call (including anything just relocated out of the code), and
`Closes #n` links are right.

### 5. Run everything
Run the **entire local gate** on the final head — full test suite, lint, type-check, build,
security scans, `actionlint`/`shellcheck` where workflows or scripts changed
(`${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md`). In a coding-session this run may be delegated
to a gate worker to keep the conductor's context lean — the *judgment* stays inline, the *command
execution* may not be. Note anything that genuinely can't run in the container as unverified, per
`${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md`.

### 6. Disposition
- **Findings needing real code changes** → through the normal fix loop (in a session: fix workers
  on the build skills; standalone: apply via the build skills), then **re-enter at step 5** — the
  full gate re-runs on the new head, and the affected passes re-verify. Bound it and escalate to
  the human rather than thrash, per the session's loop budget.
- **Clean and fully green on the same commit** → confirm `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`
  holds end to end, post the written review on the PR (severity-ranked findings resolved, verdict,
  what the gate ran), and — in the coding-session pipeline — **flip the PR to ready** and hand off
  per the session's sign-off package. Standalone: report the verdict and stop; flipping is the
  owner's call.

## What this skill does NOT do
- Run as a subagent, or let the final pass be delegated wholesale — the live agent reviews; only
  gate/fix execution may be dispatched.
- Replace `code-review` — per-step and interactive diff reviews stay that skill's job.
- Sample the tests, skip slow suites, or reuse a green from an earlier head.
- Delete a comment whose information still matters without first moving it to the PR or issue.
- Judge a layer without loading its skill, or skip the repo's own `CLAUDE.md`.
- Merge, approve past a red gate, or flip a PR whose review and green don't hold on the same commit.
