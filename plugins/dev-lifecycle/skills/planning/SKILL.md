---
name: "planning"
description: "Produce a clear, actionable implementation plan before any code is written, then — once the user approves it — file it as a GitHub issue and hand it to the build pipeline. Use this skill WHENEVER the user asks to plan, scope, design, or \"figure out how to approach\" anything — a new project, a feature, a refactor, a performance push, a bug fix, or a technical investigation. Trigger it even when the user doesn't say the word \"plan\" but is clearly asking how something should be built or fixed (e.g. \"how would we add X\", \"what's the best way to tackle Y\", \"I need to fix this bug\"). Planning is investigation and design only — it never writes or runs implementation code. It gathers context efficiently, proposes an approach, iterates with the user to approval, then records the approved plan as a GitHub issue."
---

# Planning

A plan is a thinking artifact, not a coding session. Understand the problem, learn what already exists, lay out a concrete path forward — then stop, get the user's approval, and only then file it. No implementation code is written here; none of it would be run or reviewed at this stage, so writing it now just burns context and pre-commits to decisions the plan hasn't justified.

Planning is the entry point to the pipeline: the approved plan becomes a GitHub issue. So the plan is also the build agent's brief — it must be clear enough to implement from.

## Core rules

- **Investigate, don't implement.** Read and search the codebase as needed. Do not write feature code, scaffolding, or migrations. Tiny illustrative snippets (a few lines showing an interface shape or a data model) are fine when they make the plan clearer; full implementations are not.
- **Work context-efficiently.** Context is the budget. Locate with search before reading; read the specific spans that change the plan, not whole files or directories; state reasonable inferences as assumptions rather than verifying everything. See `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`.
- **Stop for approval.** Present the plan and iterate on feedback. Do NOT file the issue until the user explicitly approves. The plan is the deliverable; the user's approval is the trigger.
- **Check the plan against the open backlog.** Every plan is cross-checked against the repo's open issues and PRs by a `sonnet` subagent before the user is asked to approve it, so what the work closes, duplicates, or conflicts with is on the table at the gate rather than discovered after merge (`${CLAUDE_PLUGIN_ROOT}/shared/issue-cross-check.md`).
- **Detailed but concise.** Every section earns its place. A senior engineer — or the build agent — should be able to read the plan and start building. Cut throat-clearing, restating the obvious, and filler.

## Workflow

### 1. Classify the request

Identify which kind of plan this is, because it changes what context matters and how the plan is shaped:

- **Greenfield project** — nothing exists yet. Focus on architecture, stack choices, and the initial build order. There's no codebase to read.
- **New feature / push** — adding to an existing codebase. The bulk of the work is understanding what's already there and where the new work plugs in.
- **Bug fix** — something is broken. The plan centers on root-cause investigation, not just the surface symptom.
- **Refactor / migration** — changing structure without (ideally) changing behavior. Emphasis on blast radius, sequencing, and how to keep things working throughout.

If the request is ambiguous about scope, ask one focused clarifying question before investigating — but only if the ambiguity would meaningfully change the plan. Otherwise proceed and note the assumption.

### 2. Gather context (skip for greenfield with no codebase)

Understand the problem and the current state of the relevant code well enough to propose a sound approach — and no more. Efficiency matters most here (see `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`).

- Locate the relevant area with search before reading — directory structure, file/function names, grep for symbols. Then read only the specific code the change touches or depends on, not whole files or trees.
- Trace the relevant data flow: where the data comes from, what transforms it, where it ends up.
- Note existing patterns and conventions (how similar features are built, naming, error handling, test style) so the plan fits the codebase rather than fighting it.
- For bug fixes, find the actual mechanism, not just the symptom. Form a root-cause hypothesis and identify the evidence for it.
- Stop gathering once each plan section can be written with justified confidence. If a detail can't be resolved cheaply, surface it as an open question rather than spelunking.

### 3. Write the plan

Compose the plan using the structure below. Adapt section depth to the size of the work — a one-line bug fix doesn't need the heft of a new service. Omit a section only if it genuinely has nothing to say.

```
## Goal
One or two sentences: what we're building/fixing and why it matters.

## Current state & context
What already exists that's relevant. For brownfield: the specific files,
functions, models, and patterns involved, with paths. For bug fixes: the
root cause and the evidence for it. For greenfield: the chosen stack and
high-level architecture, with brief rationale.

## Proposed approach
The strategy at a conceptual level before the step list — the key design
decisions and why. Call out alternatives considered and why they were
rejected, when the choice isn't obvious.

## Step-by-step breakdown
An ordered list of concrete, reviewable steps. Each step names what
changes (which files/modules/endpoints), and is small enough to verify on
its own. This
is the heart of the plan and the build agent's checklist. The whole list
should land as ONE reviewable PR — if it can't (roughly a thousand-plus
changed lines, or several unrelated subsystems), say so and propose
splitting the work into separate issues instead.

## Execution plan (dependencies & parallel tracks)
The step list in dependency order rather than narrative order, per
${CLAUDE_PLUGIN_ROOT}/shared/parallelization.md. For each step, what it
actually depends on (another step's output — a contract, a migration, a
type) and which files it touches. Group steps with no dependency path
between them and no file overlap into parallel tracks, and name the join
points where tracks must converge. Pull a small unblocking step (an API
contract, a schema) forward when it opens up tracks. State it compactly,
e.g. `Track A: 1 → 2 → 4 | Track B: 3 → 5 (needs A:2) | Join: 6`. If the
work is genuinely a serial chain, say so — but say it because the graph
says so, not because the list was written in that order.

## Related issues
What this plan touches in the repo's open backlog, from the cross-check in
step 4: issues it closes, partially addresses, duplicates, or conflicts
with — `#n — bucket — one line — recommended action`. State "None found"
when nothing intersects; omit the section only when there's no GitHub.

## Risks & open questions
Things that could go wrong, decisions that need the user's input, unknowns
that couldn't be resolved cheaply, and anything that would change the plan
if the answer were different.

## Human checkpoints
Mid-build points where a human must be pulled in, if any: manual test
gates only a person can run, known decision points, or irreversible /
externally visible actions (shared-data migrations, deploys). The default
is NONE — state "None" when the build can run from approval to final
review without stopping. In a coding session these are the only planned
mid-flight stops, and the user approves them as part of the plan.

## Acceptance criteria
How we'll know it's done and correct. Derive these from the shared
merge-ready bar in ${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md — observable behavior,
the edge cases to cover, and what testing the implementation should
include. These become the build agent's target and the testing skill's
checklist.
```

### 4. Cross-check the plan against the open backlog

A plan is written against the code, but it lands in a repo that already has a backlog. Before the user sees it, dispatch a **`sonnet` subagent** to sweep the repo's **open** issues and PRs for what this plan **closes**, **partially addresses**, **duplicates**, or **conflicts with**, and fold its ranked list into the plan's `## Related issues` section. The brief and the bucket definitions are `${CLAUDE_PLUGIN_ROOT}/shared/issue-cross-check.md`.

It's read-only and cheap: it never comments on, labels, closes, or opens an issue, and it runs concurrently with any other verification pass rather than delaying the draft. Two findings change what happens next — a **duplicate** goes to the user before anything is filed (step 6 must not open a second issue for tracked work), and a **conflict** is raised at the gate as an explicit decision rather than resolved unilaterally. No GitHub, or nothing intersecting: one line saying so, and move on.

### 5. Review with the user and get approval (the gate)

Present the plan in the conversation. Discuss, adjust, and iterate on the user's feedback until they **explicitly approve**. This is the back-and-forth, and it may take several rounds. Do not file anything or trigger the build during this step. If the user requests changes, revise and re-present. Only explicit approval moves to step 6.

### 6. Record the approved plan

On approval, and only then:

- **File a GitHub issue.** Title from the goal; body is the plan (including its `## Related issues` findings); render the step-by-step breakdown as a markdown task list (`- [ ]`) so progress can be checked off. Where the cross-check found an existing issue this work fully delivers, note it so the build's PR carries a `Closes #n` for it — and where it found a **duplicate**, don't file at all until the user has said whether to use the existing issue instead. Prefer `gh issue create`; fall back to the GitHub API. For a large effort, a tracking issue with linked sub-issues is fine, but a single well-structured issue is the default. This shape mirrors the repo's `.github/ISSUE_TEMPLATE/feature.yml` (or `bug.yml` for a fix) issue form — someone filing manually through the GitHub UI ends up with the same structure, so hand-filed and skill-filed issues stay in sync.
- **If this issue belongs to an epic** (a `product-planning` roadmap stage, or any tracking issue), link it so the epic reconciles itself when the work merges:
  - Register it as a native **sub-issue** of the epic (`gh api` / GitHub's sub-issues endpoint, or the `sub_issue_write` tool) — this alone moves the epic's progress bar when the issue closes, no automation required.
  - Add an `Epic: #<n>` marker line to this issue's body, and make sure the epic's checklist line for this stage carries this issue's number, e.g. `- [ ] Stage 3 — Auth (#<this-issue>)`. That pair is what the `epic-checkoff` workflow keys on to flip `- [ ]` → `- [x]` in the epic when the issue closes (via the merged PR's `Closes #`). Without the marker and the number on the line, the box won't tick.
- **If GitHub isn't available** (no `github.com` remote, or the CLI/API can't create issues), present the plan inline, say plainly it couldn't be filed, and note the build wasn't auto-triggered. Never silently drop the plan.

Filing the issue is recording and delegating, not implementing — this does not violate "investigate, don't implement." Planning still writes no code; the build agent does.

### 7. Hand off

Share the issue link and a one-line summary.

**Where you sit in the chain:** discovery → technical-proposal → web-proposal-writer → product-planning → planning.

## What this skill does NOT do

- Write production code, scaffolding, config, or migrations.
- Run code, tests, or commands that mutate state — beyond creating the issue once the user has approved.
- File the issue or trigger the build before the user approves the plan.
- Present a plan without saying what it closes, duplicates, or conflicts with in the open backlog — or let the cross-check write to another issue.
- Read whole files or directories when a targeted search and a specific span would do.
- Pad the plan with generic best-practice boilerplate not specific to this work.
