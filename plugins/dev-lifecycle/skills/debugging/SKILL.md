---
name: "debugging"
description: "Systematically diagnose and root-cause a failure — a crash, an error, a wrong result, a flaky test, a performance problem, or a production incident — then fix it at the root and prevent recurrence. Use this skill WHENEVER something is broken or behaving wrong and the cause isn't yet known: \"why is this failing\", \"this error keeps happening\", \"X returns the wrong value\", \"the app is down\", \"this test is flaky\", \"debug this\", \"track down this bug\". This is investigation of an *observed* failure (distinct from the planning skill, which scopes an already-understood problem). For a live production incident, it mitigates user impact first, then investigates."
---

# Debugging

Find out *why* something is broken — with evidence, not guesses — then fix the actual cause and make sure it can't come back silently. The discipline that separates debugging from flailing is being **hypothesis-driven**: observe the failure, form a specific theory, predict what you'd see if it were true, test that prediction, change one thing at a time. Randomly editing code to "see if it works" masks bugs and teaches you nothing.

The other half is honesty about cause: a fix that makes the symptom disappear is not the same as a fix that addresses the root cause.

## Core rules

- **Reproduce first.** You can't confirm a cause or a fix for something you can't reproduce. Pin down the exact symptom (expected vs actual) and the trigger conditions before theorizing.
- **Evidence before theory.** Read the actual error, stack trace, and logs. *Locate* where it fails before guessing *why*.
- **One variable at a time.** Change one thing, observe, conclude.
- **Ask what changed.** For something that used to work, the highest-signal question is what changed (code, dependency, data, config, environment). `git bisect` finds regressions fast.
- **Root cause, not symptom.** Keep asking "why" until you can explain the full causal chain and toggle the bug on and off deliberately.
- **Mitigate before you investigate (production).** If live users are harmed, stop the bleeding first — roll back, flip a flag, fail over — then root-cause at a sane pace.
- **Close the loop, and prove it closed.** Once fixed, add a regression test and *demonstrate* it red before green — revert the source (never `git stash push <paths>`, which no-ops on a committed tree), watch the test fail with the message the bug predicts, restore, watch it pass, record both. A fix that was never seen failing is unproven (`${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md`).
- **Work context-efficiently.** Localize with search and targeted reads; isolate a heavy investigation in a subagent so its exploration doesn't flood the main context. See `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`.

## Workflow

### 0. Triage (production incidents only)
Assess user impact, **mitigate immediately** (roll back to the last good SHA, flip a flag, scale, fail over — see the devops rollback conventions), confirm the bleeding stopped, then investigate. The mitigation is temporary; the root-cause fix still follows.

### 1. Understand & reproduce
Capture the precise symptom (exact error, expected vs actual, where). Establish a reliable reproduction, then **minimize** it — a minimal repro often reveals the cause by itself.

### 2. Gather evidence
Read the stack trace both directions (where it threw, and the path there) and the surrounding logs. Inspect state at the failure point (debugger, targeted logging). Check what changed recently if it's a regression. See `${CLAUDE_PLUGIN_ROOT}/references/debugging/methodology.md`.

### 3. Hypothesize & test
Form a *specific, falsifiable* hypothesis, predict what you'd observe if true, then test it — instrument the spot, run the minimal repro, or **bisect** to narrow (code path, input, history, or time window). If refuted, discard and form the next.

### 4. Isolate the root cause
Narrow until you can explain the complete chain and reproduce-on-demand by toggling the cause. For layer-specific causes, see `${CLAUDE_PLUGIN_ROOT}/references/debugging/debugging-by-layer.md`.

### 5. Fix & verify
Fix the **root cause** to the relevant build skill's conventions (frontend/backend). Verify the original repro passes and the blast radius is intact. **Add a regression test** (testing skill). For an incident, confirm the temporary mitigation can be removed and write a brief post-incident note (the documentation skill can formalize it).

## How this works with the other skills
- **devops** observability supplies evidence and rollback is the mitigation. **testing** turns every fix into a regression test. **planning** scopes a larger fix from a confirmed root cause. **frontend/backend** implement; **code-review** reviews. **documentation** captures post-incident write-ups.

## What this skill does NOT do
- Change code without a hypothesis. Patch the symptom and declare victory. Skip reproducing or confirming the fix. Investigate at leisure during an outage. Leave a fixed bug without a regression test.
