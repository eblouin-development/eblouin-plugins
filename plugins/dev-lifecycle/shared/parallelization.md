# Parallelization

The canonical reference for deciding **what work can run at the same time** and how to run it
safely. Orchestrating skills point here; a coding session applies it by default on every feature.

Governing idea: **sequence is a cost, not a default.** Wall-clock time is the user's scarcest
resource in a session, and most step lists are ordered by narrative convenience rather than by
real dependency. Before executing a list, work out the dependency graph and run everything the
graph allows to run concurrently. Sequence only where a genuine dependency, a shared file, or a
safety rule demands it.

The counterweight: parallel workers that touch the same files produce conflicts, lost work, and
review archaeology. Parallelism is the default *ambition*; isolation is the price of admission.

## The parallelization pass

Do this once per plan, before any build starts, and re-do it whenever the plan changes.

1. **List the units of work** — the plan's steps, or the investigations you're about to run.
2. **Draw the edges.** For each unit ask: does it need another unit's *output* (an API contract, a
   migration, a type, a component that must exist)? That's a real dependency. "It feels natural to
   do A first" is not.
3. **Check the file overlap.** Two independent units that edit the same files are still
   serializable-only unless they're isolated in separate worktrees. Predicted overlap is a
   dependency edge for planning purposes.
4. **Group into tracks.** Units with no path between them and no file overlap form parallel
   tracks. Each track is internally ordered; tracks run concurrently.
5. **Name the join points.** Where tracks must converge (integration step, a test suite that needs
   both sides, the final review), mark them explicitly — they're barriers, everything before them
   must land first.
6. **Record it.** The dependency graph and the resulting tracks go in the plan / issue, so the
   parallelism is reviewable and survives a session restart.

Express the result compactly, e.g.:

```
Track A (backend):  1 → 2 → 4
Track B (frontend): 3 → 5        [depends on A:2 — the API contract]
Track C (docs/tests for 1): parallel with everything after 1
Join: step 6 (integration + e2e) needs A and B complete
```

## What is almost always parallelizable

- **Read-only investigation.** Codebase surveys, stack detection, prior-art searches, "how is X
  used here" — fan these out; they can't collide by construction. This is the cheapest and most
  underused win.
- **Independent subsystems.** Backend endpoint work and an unrelated frontend screen; two
  unrelated modules; separate services in a monorepo.
- **Work behind a settled contract.** Once the API shape / schema / type is fixed and written
  down, producer and consumer build against it simultaneously. **Deliberately front-load the
  contract step** to unlock this — a small first step that fixes the interface converts a serial
  chain into two tracks.
- **Docs, tests, and seed data for already-built code.** These follow the code but don't block
  each other or the next feature step.
- **Review of step N while step N+1 builds** — when N+1 doesn't build on N's reviewed output.
- **Multiple independent findings being fixed** after a review, when they sit in different files.

## What must stay sequential

- **Real output dependencies.** A migration before the code that queries the new column; a
  contract before its consumers; a refactor before work layered on it.
- **Same-file edits**, unless the workers are in separate worktrees and you accept an integration
  merge.
- **Anything sharing a mutable resource** — one database, one dev server port, one external
  sandbox account — unless each worker gets its own.
- **Git operations on one branch/worktree.** Never two writers in one working tree, ever.
- **Irreversible or externally visible actions** (deploys, shared-data migrations, anything at a
  declared human checkpoint). These serialize through the human.
- **When you're unsure.** An unclear dependency is a dependency. Serializing costs minutes;
  a bad concurrent write costs the step.

## Running tracks safely

- **One writer per working tree.** Two build agents may run concurrently *only* in separate git
  worktrees (`Agent` with `isolation: "worktree"`, or an explicit `git worktree add`). Two agents
  in one tree is never allowed, no matter how disjoint the files look.
- **Integrate at the join, one at a time.** Each track lands on the shared feature branch through
  a single sequential merge at its join point, with the orchestrator resolving conflicts. Parallel
  *build*, serial *integration*.
- **Brief each worker with its track's boundary.** Tell it which files/modules are its own and
  which belong to a sibling track it must not touch. An unbounded worker will wander into the
  other track's files.
- **Dispatch all of a wave in one message.** Concurrent workers must be spawned in a single
  assistant turn — spawning them one per turn is sequential with extra steps.
- **Watch them under the normal cadence.** Each concurrent worker still gets its own watchdog per
  `${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md`; a wave is done when its slowest member returns.
- **Don't fan out past what you can supervise.** Three to four concurrent build workers is a
  practical ceiling; beyond that the orchestrator's integration and review burden outweighs the
  wall-clock saving. Read-only investigators can go wider.
- **Cap the blast radius.** If a track fails or needs rework, the other tracks' work must still be
  landable. If they aren't independent enough for that, they weren't parallel tracks.

## Efficiency beyond parallelism

Parallelizing is one lever; the goal is the shortest sound path to done. Also, by default:

- **Batch independent tool calls** into one message rather than one per turn.
- **Front-load the unblocking work.** When one small step (a contract, a type, a schema) unblocks
  several others, do it first even if the plan listed it later.
- **Do the cheap disqualifying check first.** If a five-second check could invalidate an hour of
  work, run it before the hour.
- **Don't parallelize what you shouldn't be doing at all.** Cutting a step beats running it
  concurrently.

## Anti-patterns

- **Parallel-washing a serial chain.** Declaring steps independent because you want the speed,
  then discovering the dependency at integration.
- **Two agents, one branch.** The classic corruption path. Worktree or wait.
- **Fanning out before the contract exists.** Consumers built against a guessed interface get
  rewritten — that's negative parallelism.
- **A wave sized to impress.** Eight concurrent workers whose output the orchestrator can't
  integrate is slower than three it can.
- **Silent parallelism.** Running tracks the plan didn't declare, so the user can't tell what was
  built concurrently when they review it.

## See also

- `${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md` — how to watch the workers you dispatch.
- `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md` — spawning subagents to keep context lean.
