---
name: "coding-session"
description: "Run the build of a whole feature end to end — from scoping (or picking up an already-scoped feature) through build and review to a single merge-ready PR — by orchestrating the firm's lifecycle skills as subagents, self-directing between the human gates. The human is in the loop at scope approval, at any checkpoints the approved plan declared (manual test gates, known decision points), at genuine escalations, and at final review and sign-off — never for per-step merges. Use this skill WHENEVER the user wants to drive a feature or project from start to finish in one sitting rather than one step at a time: \"start a coding session\", \"let's build this end to end\", \"take this from plan to merge\", \"pick up this epic and build it\", \"run the whole pipeline on this\", \"work through this issue and keep going\". It is the conductor: it scopes (or picks up scoped work), files/updates the GitHub issue and marks it in-progress, then advances step by step autonomously — each step built by a build subagent (frontend/backend/etc.) as commits on one feature branch under one draft PR, reviewed internally before the next — keeping a decision log as it goes; when the final whole-PR review is clean it flips the PR ready and notifies the user to review, sign off, and merge. It never merges — the human merges — and between the two gates it stops only for declared checkpoints or decisions that genuinely need the human."
---

# Coding session

A coding session is the conductor for the build of **one feature**. The individual skills — `planning`, `frontend`/`backend`, `testing`, `code-review` — each do one job well; a coding session strings them together into the full **scope → issue → build → review → sign-off → merge** loop and runs it, so a feature goes from idea (or an already-scoped issue) to a single merged PR without you hand-carrying each step.

It orchestrates by **spawning subagents**, one per stage of work, each briefed to invoke the right firm skill with a focused context. The conducting thread stays lean — it holds the plan, the issue and PR numbers, the step checklist, and the decision log — while the heavy lifting (reading the codebase, writing code, reviewing a diff) happens in subagents whose context is thrown away when they finish. That's the token-efficiency doctrine at the session level: the orchestrator remembers *what* and *where*, the subagents do the *how* (`${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`).

There are exactly **two standing human gates**: the user approves the scope (and with it, the autonomy contract — where the session will and won't stop) before any code is written, and the user reviews, signs off on, and merges the finished PR. Between those gates the session self-directs: it builds the feature step by step on one branch, reviewing each step internally, and stops **only** at a checkpoint the approved plan declared or at a decision that genuinely needs the human. Steps never get their own PRs or their own merges. The session never merges — merge is the human's call (`${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`).

## This is the pipeline

The firm runs the whole plan → build → review → merge loop through this skill — an interactive session driven from a thread using **local subagents**, so a human watches it happen, is kept in the loop at the gates, and flows straight into the next feature when a PR merges. There is no separate headless path: GitHub mentions and PR/issue events no longer trigger any build or review Action. Every feature is picked up and built the same way — through a coding session, run locally or from Claude Code on the web.

## Core rules

- **Two standing gates, and only declared stops between them.** Gate 1: the user approves the scope and the autonomy contract before any code. Gate 2: the user reviews and merges the finished PR. Between them, stop only at a checkpoint the approved plan declared, or to escalate a decision that genuinely needs the human. Never invent ceremony stops; never skip the two gates.
- **One feature, one branch, one PR.** The session's unit is a feature. Its internal steps land as commits on a single feature branch under one draft PR — a step never gets its own PR or its own human merge. For an epic, each stage is a feature: one session pass, one PR, merged before the next stage starts.
- **Size-guard the PR at gate 1.** If the feature can't land as one reviewable PR (roughly a thousand-plus changed lines, or several unrelated subsystems), the plan must say so at gate 1 and propose splitting it into separate features/sessions. Splitting is a scoping decision made once, up front — not incremental merges imposed mid-build.
- **Steps advance autonomously.** Build a step, review it internally, fix the blockers, tick its box on the issue, push, move on. The user is not summoned between steps.
- **Keep a decision log.** Every judgment call made without the user — a choice between viable approaches, an assumption resolved unilaterally, a deviation from the plan — is recorded in the PR description's `## Decision log` as it happens: "chose X over Y because Z." This is what makes gate 2 an informed review rather than a rubber stamp on a diff the user didn't watch being built.
- **Orchestrate, don't inline the work.** Spawn a subagent for each build step and each review and let it invoke the firm skill. Don't write feature code or run the review yourself in the conducting thread — that bloats the orchestrator's context and defeats the point. The conductor reads the codebase only enough to write good subagent briefs.
- **The repo is the memory.** State lives in GitHub — the feature issue and its ticked step checklist, the `in-progress` label, the draft PR with its commits and decision log — not in the thread. Push after every completed step. If the session is interrupted, another session can pick the feature up from the branch, the issue, and the PR alone.
- **Merge-ready is the ceiling, never merge.** The session converges on `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`: behavior meets acceptance criteria, meaningful tests pass, CI green, security clean, the final review's blockers resolved. Then it flips the PR to ready, **notifies the user, and stops**. No agent self-merges.
- **Bound the loop, then escalate.** Governs *review/quality* non-convergence: if a build↔review round can't converge in a couple of passes, or a finding needs a design decision, stop and bring it to the user with the diagnosis — don't thrash or force a risky change. An escalation is signal, not failure; the contract is that every mid-flight stop is worth the user's attention. (Distinct from a worker going *silent* mid-step — that's a liveness stall, handled by the cadence rule below, not this one.)
- **Own the branch to green — CI is yours, not the human's.** Work is built on the feature branch in the container and pushed when the worker has it locally green. From that push on, the conductor drives it: watch the run, triage the red, delegate the fix, push again, repeat. Only once CI is green does the review run, posting its comments on the PR; the conductor then picks those findings up and delegates the blocking ones back to a worker, which re-enters at CI. Converged means **green and clean at the same time**, on the same commit. If CI is down or absent, the identical loop runs entirely inside Claude against the pipeline's own commands in the container (`${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md`).
- **Watch workers actively, don't wait passively.** A stalled or dropped subagent emits no completion signal, so a passive wait can leave it dead for the better part of an hour. Dispatch in the background and back every worker with a right-sized watchdog; catch stalls in minutes, never busy-poll. **A CI run needs its own watchdog too** — no completion notification is coming for it — so every push arms one, and a run that stays queued or silent past its window is treated as a stall, not something to keep waiting on. See `${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md`.
- **Find the parallelism before you execute.** A plan's step list is written in narrative order, not dependency order. Before building, run the parallelization pass — dependency graph, file-overlap check, parallel tracks, join points — and run every track the graph allows concurrently. Sequence only where a real dependency, a shared file, or a safety rule demands it. Concurrent build workers get **separate worktrees**, never the same working tree (`${CLAUDE_PLUGIN_ROOT}/shared/parallelization.md`).
- **Route each subagent to the right model.** Reasoning-heavy stages (planning, plan-review, code-review) run on a stronger model; mechanical build/implementation runs on a cheaper one. Pass the model explicitly on every spawn (see "Model routing" below) — an unset model inherits the orchestrator's, which is the most expensive default and the main source of avoidable spend.
- **Cross-check the plan against the open backlog before gate 1.** Every plan this session is about to build gets checked against the repo's open issues and PRs by a `sonnet` subagent — what it closes, what it partially addresses, what it duplicates, what it conflicts with — and the result is presented with the plan so the user approves against the real backlog (`${CLAUDE_PLUGIN_ROOT}/shared/issue-cross-check.md`).
- **Manage your own context; never run the window to the wall.** At ~75% of the context window the conductor hands off to itself at a step boundary: flush state to the issue and PR, refresh the continuation brief (the sticky comment on the draft PR), name the skills in play, compact, then reload those skills and resume at the recorded next action. This is routine maintenance — it does not stop the run, does not ask the user anything, and must not lose a live worker or the loop's round counters (`${CLAUDE_PLUGIN_ROOT}/shared/context-continuity.md`).

## Model routing

The session runs many subagents, and each is spawned with the `Agent` tool's `model` parameter. **Always set it** — leave it unset and the subagent inherits the orchestrator's model (Opus), which is the costliest option and the reason an un-routed session burns far more than it needs to. Route by what the stage actually demands:

| Subagent / stage | Model | Why |
| --- | --- | --- |
| **Orchestrator** (this conducting thread) | `opus` | Holds the plan, loop state, decision log, and gate decisions — reasoning-critical, and usually the session's own model already. |
| **Planner** (`planning` / `product-planning`, step 1) | `opus` | Investigation and design quality set the ceiling for everything downstream; cheap here is expensive later. |
| **Plan-review** (step 2) | `opus` | Judges whether a plan is actually buildable and what it glossed over — a judgment stage. |
| **Issue cross-check** (step 2) | `sonnet` | Mechanical search-and-classify over the open backlog — no design judgment, and it runs on every plan (`${CLAUDE_PLUGIN_ROOT}/shared/issue-cross-check.md`). |
| **Build / implementation** (`frontend`, `backend`, `testing`, `data`, `debugging`, `devops`, `infrastructure`; step 4 and every fix round) | `sonnet` | Mechanical execution against a concrete plan — Sonnet builds to spec well and is where the bulk of tokens are spent, so this is the biggest saving. |
| **Code-review** (per-step review, step 5; final whole-PR review, step 7) | `opus` | Correctness/security judgment — catching one real bug outweighs the token savings (`${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`). |

Rules of thumb:

- **Default reasoning/judgment stages to `opus`, execution stages to `sonnet`.** The split above is the default; follow it unless the user says otherwise.
- **The user can override per session.** If the user asks to run a stage cheaper (e.g. "review on Sonnet this time" for a low-risk change) or richer, honor it for that session — the defaults are a starting point, not a lock.
- **Match the model to the risk, not the file count.** A large but mechanical build is still `sonnet`; a small but subtle security-sensitive change may warrant keeping review (or even build) on `opus`. When a build step is unusually tricky, it's fine to raise that one spawn to `opus`.

## Context self-management

A session outlives its context window, so the conductor manages that window itself rather than being truncated by it. The doctrine is `${CLAUDE_PLUGIN_ROOT}/shared/context-continuity.md`; what it means here:

- **The continuation brief is a sticky comment on the draft PR**, refreshed in place at every step boundary — issue/PR/branch, per-track status, live workers with their task IDs and watchdogs, the CI-green/review-clean loop state and its round counters, decisions since the last refresh, open findings, checkpoints passed and ahead, and the single next action. Keeping it current is a few edited lines; it's what makes the handoff nearly free.
- **At ~75% of the window, hand off to yourself** — at a boundary (a step ticked, a wave integrated), never mid-integration. Flush to the repo, refresh the brief, name the skills in play (`coding-session` plus the shared doctrines governing the run, plus any project `CLAUDE.md` conventions), compact, then **re-invoke those skills**, re-read the brief, re-arm a watchdog per live worker, and resume at the recorded next action.
- **It is not a mid-flight stop.** A handoff is invisible to the user: no check-in, no "where were we", no re-planning of work already done. Step 6's rule stands — only a declared checkpoint or a genuine escalation reaches the human. A handoff that *can't* complete (state won't push, a worker won't drain) is the exception, and that one escalates.
- **The brief is also the interruption plan.** If the session dies outright, whoever picks the feature up — another session, or you tomorrow — resumes from the branch, the issue, the PR, and that comment. That's the same "the repo is the memory" rule, made survivable at the window boundary.

## Workflow

### 1. Start the session — scope new, or pick up scoped

Two entry points:

- **Scope new work.** The user is starting something fresh. Run the `planning` skill (or `product-planning` for a whole product) to investigate and draft the plan — spawn the planner on **`opus`** (see "Model routing"). Planning owns the investigate → draft → iterate loop; let it. Do **not** let planning file the issue yet — in a session, filing is the session's job (step 3), because the session drives the build itself with local subagents. Carry the draft plan into step 2.
- **Pick up an existing epic or issue.** The user points at an epic or issue already on GitHub. Read it (and its ADR/epic parent if any). If it's an **epic**, identify the next unstarted stage — the first `- [ ]` line / open sub-issue in roadmap order — and make *that stage* the feature for this pass; run `planning` on it if its issue is still a stub. If it's a **single issue** with an actionable plan already, use it as-is. Confirm with the user which feature you're picking up before proceeding.

Either way you arrive at **one feature** with a step-by-step plan concrete enough to build from.

### 2. Verify the plan, set the autonomy contract, get approval (gate 1)

Before it becomes the build brief, the plan must be sound. Spin up a short **plan-review subagent** on **`opus`** (brief it with the plan + the relevant part of the codebase) to sanity-check it for completeness, feasibility, missing edge cases, and blast radius the plan glossed over — a cheap pass that catches "this plan can't actually be built as written" before a build agent discovers it the expensive way. Fold its findings back into the plan.

Dispatch an **issue cross-check subagent** on **`sonnet`** in the *same message* — both passes are read-only and independent, so they run concurrently and cost one wall-clock window between them. It sweeps the repo's open issues and open PRs for what this plan **closes**, **partially addresses**, **duplicates**, or **conflicts with**, and reports a compact ranked list; the full brief is `${CLAUDE_PLUGIN_ROOT}/shared/issue-cross-check.md`. If the planner already ran this check in step 1 (it's `planning`'s own step 4), reuse its `## Related issues` section rather than repeating the sweep — re-run only when the plan changed materially since. Fold the result into the plan as a **`## Related issues`** section (or "None found"). It is read-only — it never comments on or closes another issue; the session acts on the findings only after gate 1:

- **Closes** hits become `Closes #n` links on the issue filed in step 3.
- **Duplicate** hits go to the user *before* filing — the session must not open a second issue for work already tracked.
- **Conflicts** are presented at the gate as an explicit decision. A plan that contradicts another open issue or collides with an in-flight PR is not something to resolve unilaterally.

Then run the **parallelization pass** over the step list (`${CLAUDE_PLUGIN_ROOT}/shared/parallelization.md`) — this is a default part of verifying a plan, not an optimization the user has to ask for. Work out which steps genuinely depend on another step's output, which would collide on the same files, and which are merely listed in a convenient order; group the rest into parallel tracks with explicit join points. If one small step (an API contract, a schema, a shared type) would unblock several others, pull it forward so the tracks can open earlier. Record the resulting **execution plan** — tracks, dependencies, joins — in the plan so it lands on the issue and the user can see what will be built concurrently.

Then make sure the plan carries the **autonomy contract** — the two things that define where the human will and won't be involved:

- **Declared checkpoints.** The plan explicitly lists any point where the session must stop for the user mid-build: a manual test gate (something only a person can verify — "check the OAuth flow against the real provider before we build on it"), a known decision point ("pick A or B once we see the query timings"), or anything irreversible or externally visible (a migration on shared data, a deploy). **The default is zero.** If the plan declares none, the session runs from approval to final sign-off without stopping.
- **The size guard.** If the feature won't fit one reviewable PR, the plan must say so here and propose the split (see Core rules). Don't let an unreviewable diff be discovered at gate 2.

Present the verified plan — including its execution plan, its related-issues findings, and its checkpoints (or "none") — to the user and **iterate to explicit approval**. This is gate 1: the user is approving *what* gets built **and** *where they'll be interrupted*. Do not file anything or start a build until they approve. If they request changes, revise and re-present.

### 3. Record the issue and mark it in-progress

On approval, file/update the work in GitHub in the right shape (this is `planning`'s step-6 behavior — reuse it, but the session performs it so it controls the trigger):

- **Single feature/fix** → **one issue**: title from the goal, body is the plan (including the declared checkpoints and the `## Related issues` findings from step 2), step-by-step as a `- [ ]` task list. Carry any **closes** hits into the PR body as `Closes #n` alongside the feature issue, so approving the cross-check actually reconciles the backlog on merge. The steps live as checkboxes on this one issue — do **not** file an issue per step. If it belongs to an epic, register it as a native **sub-issue** and add the `Epic: #<n>` marker + this issue's number on the epic's checklist line, so the epic reconciles on merge (see `planning`/`product-planning`).
- **Whole product / large effort** → epic + milestones + per-stage sub-issues (that's `product-planning`); then this session builds the stages one feature at a time, each through its own full pass of this loop.
- **Picking up an existing issue** → update it in place rather than filing a duplicate.

**Mark the feature issue in-progress.** Apply an `in-progress` label (create the label if the repo doesn't have one — a distinct color, description "actively being built in a coding session"). Label the epic in-progress too while a session is advancing it. Remove the label when the feature's PR merges (step 8); the closing issue is the completion signal, the label just says "a session has this right now" and prevents two drivers colliding.

The session itself drives the build via local subagents (step 4) — there is no Action to hand off to.

### 4. Build — steps land as commits on one feature branch

Work the **execution plan** from step 2, not the raw step list: run each wave of independent tracks concurrently and serialize only where the dependency graph says to. For each step, pick the build skill from the nature of the work, then spawn a subagent to do it — on **`sonnet`** by default (build is execution against a concrete plan; see "Model routing"), raising that one spawn to `opus` only when the step is unusually subtle or security-sensitive. Dispatch it under the worker-cadence discipline — background dispatch plus a right-sized watchdog, not a blocking wait (`${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md`):

| Work is mostly… | Skill the subagent invokes |
| --- | --- |
| Client UI — components, pages, forms, styling | `frontend` |
| Server — endpoints, models, migrations, auth, jobs | `backend` |
| Both | brief the agent to use `frontend` **and** `backend`; if the step is large, prefer splitting it into a frontend sub-step and a backend sub-step and sequencing them |
| Tests are the deliverable | `testing` |
| Infra / CI / containers / deploy | `devops` (app pipeline) or `infrastructure` (hosting) |
| Data seeding or reporting | `data` |
| A diagnosed bug fix | `debugging` |

Brief the subagent with: the issue number, this step's slice of the plan and its acceptance criteria, the specific skill to invoke, the **feature branch** name, and the bar — `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`. Instruct it to:

- Build **this step only**, as commits on the shared feature branch, meeting all benchmarks — meaningful tests at the right levels, lint/type-check/tests green **locally in the container**, security clean, docs moved with the code. **Push only once that local gate passes** — pushing to let CI find what a local run catches wastes a full CI cycle per mistake. It does **not** open a per-step PR.
- **First step to land on the feature branch only** (exactly one worker per feature, never a parallel sibling): open the feature's **draft PR** when green — `Closes #<issue>` in the body so the issue (and the epic box) reconciles on merge (plus a `Closes #n` for any issue the step-2 cross-check found this feature fully delivers), a summary of the feature, an empty `## Decision log` section for the conductor to maintain, and — if this repo's own `CLAUDE.md` documents a PR-ping convention (e.g. a `cc @<owner>` line) — follow it so the owner is notified. **Draft status is the signal that the PR is not yet for the human**; it also gives the user a live window they *can* glance at, and CI runs on every push.
- Report back what it built, the commits it pushed, and anything it couldn't resolve.

**Running tracks concurrently.** Never two build agents in one working tree — that rule is absolute. Concurrent tracks are therefore run in **separate git worktrees** (spawn with `isolation: "worktree"`), each branched off the feature branch, and each briefed with the files/modules it owns and the sibling track's files it must not touch. Dispatch a wave's workers in a **single message** so they actually run at once, each with its own watchdog (`${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md`). When a track returns, integrate it onto the shared feature branch **one at a time**, resolving any conflicts in the conductor: parallel build, serial integration. Steps within a single track stay sequential on that track's tree.

Keep the conductor thread out of the file-by-file work — the subagent holds that context. If a wave's tracks turn out to be entangled (repeated conflicts, one track blocked on another's output), collapse them back to sequential for the rest of the feature and note it in the decision log. Between waves, if `main` has moved, bring the feature branch up to date (rebase or merge `main`) so the final PR never goes stale.

### 5. Review each step internally before advancing

After each step's build returns, spawn a review subagent on **`opus`**, briefed to invoke the `code-review` skill on **that step's diff** (the commits since the last reviewed point) and **report its findings back to the conductor** — not as PR comments. Mid-build commentary on a draft PR would bury the final review the human actually reads; per-step findings are working state, and the conductor holds them. Spawn this one under the same worker-cadence discipline as a build step (`${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md`).

Reviews are read-only, so they parallelize freely: review a completed step while the next independent step builds, and review a wave's tracks concurrently — one review subagent per track's diff, dispatched together.

- **Blocker/high findings** → spawn a build subagent (**`sonnet`**) to fix them on the feature branch, then re-review. Findings in different files can be fixed by concurrent workers under the worktree rule; findings in the same file go to one worker. Bound it: after ~2 rounds without convergence, or the moment a finding needs a human decision, escalate (step 6).
- **Clean** → tick the step's checkbox on the feature issue, append any judgment calls to the PR's decision log, refresh the continuation brief (`${CLAUDE_PLUGIN_ROOT}/shared/context-continuity.md`), push, and advance to the next step (back to step 4). This boundary is also where a context handoff goes if the window is near 75% — state is clean and nothing is mid-integration.

**Every push is watched.** Once the draft PR exists, each push runs CI. Arm a CI watchdog on it (`${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md`) and don't advance a step past a red run: triage it, delegate the fix, push again. A step's box is ticked when its review is clean *and* the run on that commit is green.

This is the drift-catcher: each step is verified before the next builds on it, so the final whole-PR review confirms an already-sound feature instead of discovering three steps of compounded problems.

### 6. Mid-flight stops — declared checkpoints and escalations only

Exactly two things interrupt the autonomous run:

- **A declared checkpoint** from the approved plan. When the step list reaches one, stop and present it: what the user needs to verify or decide, and exactly how (the branch to pull, the URL to hit, the command to run, the two options and their trade-offs). Wait for their answer, fold it in, resume.
- **An escalation.** A finding or a build problem needs a human call — a design trade-off, an ambiguous requirement, an architectural decision, a review loop that isn't converging. Stop and bring the specific blocker with your diagnosis and a recommendation. A back-and-forth that isn't converging is a signal to pull the human in, not to spin.

Nothing else stops the run. If the plan declared no checkpoints and nothing escalates, the user hears nothing between gate 1 and gate 2 — that silence is the feature working as designed, and it's what keeps every actual stop meaningful.

### 7. Converge the PR — CI green, then review clean

When every step is built, step-reviewed, and ticked, the conductor drives the PR to convergence: **green on CI and clean on review, on the same commit**. This is a loop the session owns end to end — the user is not the message bus between CI and the fixer. The full doctrine, including triage and the offline fallback, is `${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md`.

**7a. Get CI green first.** After the last push, arm a **CI watchdog** sized to the repo's typical pipeline duration — a CI run is not harness-tracked work, so no completion notification is coming for it — then check the run non-blockingly and re-arm with backoff while it's still running.

- **Red** → triage before delegating: ours-and-deterministic (test/lint/type/build failure caused by the diff) → spawn a fix subagent (**`sonnet`**) with the failing job's log excerpt and the local repro command; ours-and-flaky → treat a flake this change introduced as a real finding, not a re-run candidate; not-ours (fails the same on the base branch, upstream outage, expired credential) → note it once on the PR and re-check when the base recovers. Fix, push, and the loop re-enters here. Bound it: ~3 rounds on the same failure, then escalate (step 6).
- **Green** → proceed to the review.

Don't spend a review pass on a red branch — its findings would be tangled with build breakage and half the code may be rewritten anyway.

**7b. Review the green branch, comments on the PR.** Spawn a review subagent on **`opus`**, briefed to invoke `code-review` in **pipeline mode** on the full PR diff. This pass judges the integrated feature — cross-step consistency, the complete behavior against the issue's acceptance criteria, security across the whole change — and **posts its findings on the PR**: per-finding comments with file:line, severity-ranked, plus a single summary comment with the verdict. This is the written review the human's sign-off leans on. Same worker-cadence discipline: background dispatch, its own watchdog.

**7c. The conductor picks the review back up.** Read the posted findings and decide what's blocking — that triage is the conductor's job, not the human's.

- **Changes needed** → spawn a fix subagent (**`sonnet`**) for the blocker/high findings (concurrent workers in separate worktrees when the findings sit in different files; one worker per file). It pushes when locally green, and the loop **re-enters at 7a** — CI runs again, then the review re-checks the affected findings. Never declare convergence on a green from before the last fix push. Bound it: ~2 review-fix rounds without convergence, or any finding that needs a design decision, and escalate (step 6).
- **Clean and green together** → verify the full definition of done holds (CI green on the PR head, acceptance criteria met, checkboxes all ticked), finalize the decision log, and **flip the PR from draft to ready**. Ready-for-review status is the machine-readable "this is now for you" signal.

**If CI is down, absent, or unreachable, the loop stays inside Claude — it does not pause and does not go to the human.** Reconstruct the gate from the workflow files (or the project's own documented commands) and run those same checks in the container, in a worker with its own watchdog. Same rounds, same bounds, review still runs after local green. Say plainly on the PR and in the sign-off package that the gate was run locally and which checks ran — a human must never read "green" and assume CI produced it — and re-run through CI once it's back before the PR is treated as merge-ready.

### 8. Sign-off and merge (gate 2)

Notify the user in the thread that the feature is ready. Give them a **sign-off package**, not just a link:

- The PR link and a one-paragraph summary of what the feature does.
- The **decision log** — every judgment call made without them, so they can push back on any of it.
- **Verification evidence**: CI green on the PR head (or, if CI was unavailable, an explicit statement that the gate was run locally in the container and exactly which checks ran), final review clean, acceptance criteria checked off.
- **Anything only a human can verify** — from the declared checkpoints or surfaced during the build (e.g. "worth clicking through the new flow on staging before merging").
- For an epic: which stage this is and what's left.

Then **stop and wait**. This is gate 2; the session does not merge. If the user requests changes, treat them as a fix round through the same loop: build subagent applies them and pushes, CI re-runs and must go green, the final review re-verifies, then re-notify (step 7). Once the user merges, the PR's `Closes #<issue>` closes the feature issue, which (via the sub-issue link + `epic-checkoff`) ticks the epic's box. Remove the `in-progress` label.

### 9. Advance to the next feature, or close out

- **Epic with stages remaining** → return to step 1's "pick up scoped" path for the next `- [ ]` stage and run the full loop again — new feature branch, new PR, its own gate 1 and gate 2. Announce each advance so the user always knows which feature is active and how much remains.
- **Single feature, done** → close out: confirm the issue closed (and the epic box ticked, if any), remove any lingering `in-progress` labels, and give the user a short wrap — what shipped, the merged PR, and any follow-ups that surfaced but were left out of scope (file them as issues rather than dropping them).

## Subagent briefing notes

- **One skill focus per subagent.** A subagent is briefed to invoke a specific skill on a specific step or PR. Don't hand a subagent the whole session — hand it one stage of it. Its context dies when it returns; only what it reports (commits pushed, a verdict, a blocker) survives into the conductor.
- **Set the model on every spawn.** Pass `model` on each `Agent` call per the "Model routing" table — `opus` for planner/plan-review/code-review, `sonnet` for build/implementation. An unset model silently inherits the orchestrator's (Opus); that inheritance is the single biggest source of avoidable session cost.
- **Pass pointers, not payloads.** Give the subagent the issue number, the branch, and the step's slice of the plan, and let it read what it needs from GitHub and the repo. Don't paste whole files into the brief — that's the orchestrator paying for the subagent's reading.
- **Per-step reviews report to the conductor; only the final review posts on the PR.** The human's review at gate 2 should open onto one clean, current written review — not an archaeology dig through per-step bot commentary.
- **Build agents share the feature branch sequentially.** One writer per working tree, always. Concurrent tracks each get their own worktree and integrate onto the feature branch one at a time (`${CLAUDE_PLUGIN_ROOT}/shared/parallelization.md`).
- **Dispatch a wave in one message.** Workers meant to run concurrently must be spawned in a single assistant turn — one spawn per turn is sequential execution wearing a parallel costume.
- **A fix worker gets the evidence, not the symptom.** Brief it with the failing job's name, the relevant log excerpt, and the command that reproduces the failure in the container — not "CI is red." Fix workers are the same build skills on **`sonnet`**, and they push only once the local gate passes.
- **The review worker reviews; the conductor routes.** When `code-review` runs as this session's review worker it posts its findings and returns them — it doesn't drive its own fix loop. The conductor decides what's blocking and dispatches the fix, so one thread owns the loop and its round budget.
- **The cross-check worker searches, it doesn't act.** Brief it with the plan and `${CLAUDE_PLUGIN_ROOT}/shared/issue-cross-check.md`, and tell it explicitly that it may not comment on, label, close, or open an issue. It returns a ranked list; the conductor decides what becomes a `Closes` link, what goes to the user, and what gets dropped.
- **Fan out read-only investigation freely.** Codebase surveys, prior-art checks, and "how is X used here" questions can't collide; run them concurrently and keep their output out of the conductor's context.

## What this skill does NOT do

- Merge, or advance past a ready PR without the user merging. Ever.
- Skip either standing gate — build without scope approval, or hand over without a clean final review.
- Stop mid-flight for anything except a declared checkpoint or a genuine escalation — no per-step check-ins, no PR-per-step, no asking the user to merge increments of a feature.
- Open more than one PR per feature, or run two build agents in the same working tree at once (concurrent tracks require separate worktrees).
- Run steps sequentially that the dependency graph says are independent — or declare steps parallel to go faster when a real dependency or file overlap says otherwise.
- Flip the PR from draft to ready before the final whole-PR review is clean and CI is green on the same commit.
- Hand a CI failure to the human before triaging it, or leave a red branch sitting because no one was watching the run.
- Stop the loop because CI is down — the gate moves into the container and the loop continues, stated plainly on the PR.
- Inline the build or the review into the conducting thread instead of spawning a subagent for each.
- Open a duplicate issue when picking up existing work — update in place.
- Take a plan to gate 1 without saying what it closes, overlaps, duplicates, or conflicts with in the open backlog — or let the cross-check worker write to another issue.
- Run its context window to the wall, or ask the user to help it recover — the handoff at 75% is the session's own job and is invisible to the human.
- Resume from a handoff by re-planning, re-reviewing finished steps, or dropping a worker it had already dispatched.
