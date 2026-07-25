# Worker cadence

The canonical reference for how an orchestrator watches the subagents it dispatches. Every orchestrating skill points here.

Governing idea: **the harness's completion notification is the primary signal.** When harness-tracked work finishes, you are auto-re-invoked — polling for completion is pure waste. The watchdog described below is a **backstop, not a poll**. You watch actively only to catch what the completion signal never covers: a worker that **stalls** (hangs on a tool/network call, or loops — no "done" event ever fires) or is **dropped** (dies on a terminal error, returns nothing actionable). Wait passively on those and the worker sits dead for as long as your fallback is coarse — the 30–60 minute problem. Active cadence catches it in minutes.

## The dispatch pattern
This is the heart of the doctrine, four steps:

1. **Dispatch the worker in the background.** Don't block the conductor on it.
2. **Register one fallback watchdog wake-up**, sized to the step's *expected* duration — not a recurring timer.
3. **Let the free completion notification do the normal job.** Most workers finish and re-invoke you before the watchdog ever fires.
4. **The watchdog fires only if the worker has gone silent past its expected window.** That's the exception path, not the routine one.

## On watchdog fire — a non-blocking liveness check
Three branches, and the check itself must be **non-blocking** — never block the conductor waiting on a worker that may already be dead:

- **Progressing** (output still advancing) → re-arm the watchdog and keep waiting.
- **Silent / stalled** → stop the worker, then re-dispatch with a fresh brief.
- **Actually done, notification just missed** → proceed as if the completion signal had fired.

## Interval sizing
Size the watchdog to the work — don't pick one global number. A build step runs ~8–12 minutes; a review step ~5–8 minutes; planning or investigation longer. Re-arm with backoff on each fire rather than resetting to the same interval. **Cap re-dispatches** (e.g. two) for a single step before escalating to the human — a worker that stalls twice is signal, not something to retry forever.

## Anti-patterns
- **Short-interval polling of harness-tracked work.** Wasteful, and a real cost driver — the completion notification already does this job for free.
- **Unbounded blocking waits.** The passive-wait trap: if the worker stalls, the conductor stalls with it, indefinitely.
- **Re-dispatching a stalled worker without stopping it first.** This puts two build agents in one working tree and violates the "one writer per working tree" rule. Always stop, then re-dispatch — never both at once.
- **Dispatching a wave one worker per turn.** Workers meant to run concurrently go out in a single message; otherwise you've written sequential execution with extra overhead (`${CLAUDE_PLUGIN_ROOT}/shared/parallelization.md`).

## Portability
The pattern is: dispatch → time-boxed liveness check → stop-and-re-dispatch. That's the part that travels to any harness. Concrete tool names are the mapping onto *this* harness, not the definition:

- Dispatch in the background → the `Agent` tool with `run_in_background`.
- The watchdog wake-up → `ScheduleWakeup` (or `send_later`).
- The non-blocking liveness check → `TaskList` / `TaskOutput` with `block=false`.
- Stopping a stalled worker → `TaskStop`.

## See also
`${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md` — the push → CI → fix → review → fix loop this cadence backstops, including the CI watchdog (a CI run emits no completion notification at all) and the local fallback when CI is unavailable.

`${CLAUDE_PLUGIN_ROOT}/shared/parallelization.md` — which workers may run at the same time, and the worktree isolation that makes it safe. This doc governs how you watch each of them.

`${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md` — specifically "Isolate heavy exploration in subagents." That rule tells you when to spawn a subagent; this doc governs how you watch the ones it tells you to spawn.
