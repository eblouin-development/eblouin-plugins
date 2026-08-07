# Context continuity

The canonical reference for how a long-running session survives its own context window. Any skill that runs for hours — a `coding-session`, a multi-stage migration, a whole-project audit — points here.

Governing idea: **the context window is a consumable, and running it to the wall is a self-inflicted outage.** A session that gets truncated mid-flight loses exactly the things nothing else holds — which worker is live, which round of a bounded loop it's on, which findings are still open — and the usual symptom is a session that resumes by re-planning work it already did, or quietly forgets a worker it dispatched. So the session doesn't wait to be truncated: at **~75% of the window** it performs a deliberate handoff **to itself** — flush state to durable artifacts, write a continuation brief, name the skills it's working under, compact, then reload and keep going. The human is not involved and should not be able to tell it happened.

Why 75%: the handoff itself costs tokens (reading the issue and PR, writing the brief, reloading skills). Trigger much later and there's no room to do it well; trigger much earlier and you're paying for handoffs you didn't need.

## Reading the gauge

No precise counter is guaranteed on every harness. Use whatever signals exist — the harness's own context/auto-compact warning, the length of the transcript, how many files and worker reports have been absorbed since the last handoff — and **err early**. Being 10% conservative costs one cheap comment edit; being 10% optimistic costs the loop state.

Trigger at a **boundary**, not wherever the threshold happens to be crossed: a completed step, a returned worker, a wave integrated onto the feature branch. A brief written mid-integration describes a state nobody can resume from.

## The handoff — five moves

1. **Flush state to the repo.** Push the commits, tick the step's checkbox on the issue, update the PR's decision log. `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md` already says the repo is the memory; this is where that rule earns its keep. Nothing load-bearing may exist only in the thread.
2. **Write or refresh the continuation brief.** It lives as a **single sticky comment on the draft PR**, edited in place (one per session, not a new comment each time) — the same place the next session would look anyway. It carries what the issue and the diff don't:
   - the feature issue, the PR, the branch, and the epic if there is one;
   - the execution plan with per-track status — done / in flight / not started;
   - **live workers**: what each is building, its task ID, and the watchdog armed on it with its fire time;
   - **loop state**: the PR's draft/ready state, whether the local gate is green and the review clean and on which commit, any post-flip CI run in progress and the watchdog on it, plus the round counters against their bounds (`${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md`);
   - decisions taken since the last brief, mirroring the PR decision log;
   - open findings not yet fixed, and which declared checkpoints have been passed or are still ahead;
   - **environment facts a resuming session would otherwise rediscover the hard way** — the interpreter or toolchain version the repo actually needs, services that must be started by hand, a gate that can't be run concurrently, credentials or fixtures that have to be set up first. These cost real time to re-learn and are invisible in the issue and the diff;
   - **the single next action.**

   Write it for a reader with no memory of the session — because that reader is either you after the handoff or a different session tomorrow.
3. **Name the skills in play.** List them explicitly in the brief: the orchestrating skill itself, the shared doctrines currently governing the run, and any project-local skill or `CLAUDE.md` convention that has been shaping the work. A skill's instructions are as summarizable-away as anything else in the transcript, and a session that resumes without them keeps the plan but loses the method.
4. **Compact.** Let the summary carry pointers — issue and PR numbers, branch, "the brief is the sticky comment on PR #n" — and let the durable artifacts carry the content. Don't try to preserve the transcript.
5. **Reload on the other side.** Re-invoke every skill named in move 3, re-read the continuation brief and the issue, re-arm a watchdog for each worker still recorded as live, then resume at the recorded next action. Do not re-plan, do not re-review completed steps, and do not ask the user where things stood — the brief is that answer.

## Rules

- **A handoff is never an escalation.** It's routine mechanical maintenance; it does not interrupt the human and does not count against the session's autonomy contract. The one exception is a handoff that *can't* complete — state won't push, a worker won't drain — which is a genuine escalation.
- **Never lose an in-flight worker.** Either let the wave drain and integrate before handing off, or record every live task ID and its watchdog in the brief and re-arm them after (`${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md`). A worker forgotten across a handoff is the exact failure this doctrine exists to prevent.
- **Keep the brief current cheaply.** Refresh it at every step boundary — a few edited lines in a comment that already exists. Then the 75% handoff is nearly free, and an unexpected truncation costs nothing either.
- **Summarize state, not code.** The diff is in the PR and the plan is in the issue. Re-describing them in the brief spends the context you're trying to reclaim.
- **Once per crossing.** Resuming resets nothing about the work. The next handoff happens the next time usage climbs back to the threshold, not on a fixed interval.
- **Spending less is the first defense.** Retrieval discipline and subagent isolation are what make the threshold arrive late (`${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`); this doctrine handles the moment it arrives anyway.

## Portability

The pattern is: **watch the gauge → flush to durable artifacts → write a self-addressed brief → compact → reload skills and resume.** That travels to any harness. On a harness that compacts automatically when the window fills, this doctrine's job is to guarantee the brief exists *before* that happens, so the automatic summary is a convenience rather than the only record.

## See also

`${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md` — how to reach the threshold later.

`${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md` — the watchdogs the brief records and the resumed session re-arms.

`${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md` — the loop state (green/clean, round counters) a handoff must not drop.

`${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md` — open findings recorded in the brief carry their evidence, so the resuming session re-verifies rather than re-assumes.
