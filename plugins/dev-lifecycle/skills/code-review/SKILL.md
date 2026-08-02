---
name: "code-review"
description: "Review code changes for correctness, breakage, best practices, DRYness, security, and performance/scalability, then either report what to fix (interactive) or take the change to merge-ready (in the coding-session pipeline). Use this skill WHENEVER the user asks to review code, check a diff or pull request, sanity-check changes before pushing or merging, or asks \"did I break anything\", \"is this safe\", \"look over my changes\", \"review this PR\" — and it is also the review stage a `coding-session` spawns internally. Works on live/local changes and on pull requests. By default it is read-only diagnosis; in pipeline mode it applies fixes via the build skills and re-reviews to merge-ready — but it NEVER merges. The human merges."
---

# Code review

Review the code that changed and either tell the user precisely what to fix (interactive) or drive it to merge-ready (pipeline). A good review is specific (file and line), justified (the *why*), prioritized (severity), and actionable. It is the loop-closer: planning scoped the work, frontend/backend built it, review verifies it before it ships.

## Two modes

- **Interactive (default).** A human asked for a review. Produce the structured, severity-ranked review and **stop** — read-only. Only make edits if the user then asks. Suggested fixes in the review are illustrative, not applied.
- **Pipeline / review-agent.** Running as the review stage inside a `coding-session` — either per-step (reviewing one step's commits before the session moves on) or as the final whole-PR review before the session flips the PR to ready. Review the change, apply fixes for its findings via the `frontend`/`backend`/`testing` skills, and re-review until it meets `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md` or the loop needs to escalate to the human. It **stops before merge** — the human merges. Use this mode when the context is a coding-session's internal review, not an interactive chat request.

## Core rules

- **Scope to the change.** Review what was touched since the base, plus that change's blast radius. Don't audit the whole codebase (that's the `security-audit` skill) — relevant *and* token-efficient. See `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`.
- **Read enough to judge, not everything.** Trace what the change touches — its call sites and contracts — not the whole tree.
- **Be honest and specific.** Real issues with locations and reasons. No vague "consider improving error handling." Don't pad with nitpicks, don't withhold a real blocker to be agreeable.
- **Judge against the right version and the project's conventions.** Flag a React-19 anti-pattern only if the project is on 19; a Pydantic v1/v2 mismatch against what's installed. "Best practice" = idiomatic for this stack and consistent with this codebase.
- **Never self-merge.** In pipeline mode the ceiling is merge-ready. Merging is the human's decision.

## Workflow

### 1. Determine scope (the diff)
Identify the base and the diff. Local: `git merge-base HEAD origin/main` then `git diff <base>...HEAD` (+ unstaged/staged). PR: `gh pr diff <n>` or the API. List the changed files/hunks before diving in.

### 2. Gather blast-radius context
For each meaningful change: if a signature/return type changed, check its callers; if a schema/API contract changed, check consumers (frontend calls, serializers); if shared code changed, consider dependents; check whether tests cover the change. Read only the surrounding code needed to judge.

### 3. Review across all dimensions
Evaluate the change and its blast radius against each dimension:
1. **Correctness & regression** — logic errors, unhandled cases, broken contracts, type mismatches, races, broken/missing tests. → `${CLAUDE_PLUGIN_ROOT}/references/review/review-dimensions.md`.
2. **Best practices & conventions** — idiomatic for the installed versions, consistent with the codebase. → same reference.
3. **DRY** — genuine duplication introduced; don't force premature abstraction. → same reference.
4. **Security** — every touched path against the OWASP Top 10:2025 and against `${CLAUDE_PLUGIN_ROOT}/references/security/secure-baseline.md`; flag any diff that ships an insecure default the baseline forbids. → `${CLAUDE_PLUGIN_ROOT}/references/security/owasp.md`.
5. **Performance & scalability** — N+1, unbounded queries, missing indexes, blocking calls on async paths. → review-dimensions reference.

Severity: 🔴 Blocker (breaks functionality, security hole, data loss — must fix) · 🟠 High (real bug / meaningful perf or best-practice problem — should fix) · 🟡 Medium (DRY, maintainability, missing tests) · ⚪ Nit (style/naming).

### 4a. Interactive mode — produce the review
Output a structured, prioritized review: a 1–3 sentence summary with a recommendation (approve / approve with nits / changes requested), findings in severity order (What / Why / Fix, with file:line), and a brief honest "what's good." Don't invent findings; if it's clean, say so. Stop here.

### 4b. Pipeline mode — drive to merge-ready
**If a `coding-session` dispatched you as its review worker, stop after posting the review.** Post per-finding comments (file:line, severity) plus one summary verdict comment on the PR and return the findings to the conductor — it owns the fix loop, its round budget, the flip to ready, and the CI run that flip starts (`${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md`). Two threads driving the same loop double-push and blow the bounds. **Never flip the PR's draft/ready state yourself** — that's the conductor's hand-off signal, and in a gated repo it's also what triggers CI.

Otherwise (pipeline mode without a conductor): apply fixes for 🔴/🟠 findings via the `frontend`/`backend` skills (and `testing` for missing tests), then **re-review the changed code** — fixes can introduce new issues. Iterate until the change meets `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`. Because CI doesn't run on a draft PR, the gate you iterate against is the **local gate** — the pipeline's own checks run in the container — and the PR is flipped to ready only once that's green and the review is clean. Bound the loop: if it can't reach merge-ready in a couple of passes, or a finding is design-level or ambiguous, **stop and escalate to the human** with the diagnosis rather than thrashing or forcing a risky change. When merge-ready, approve the PR and stop — do not merge.

### 5. Hand off
Interactive: the recommendation and the must-fix shortlist. Pipeline: the approval, a summary of fixes applied, and confirmation it's merge-ready and awaiting the human's merge — or, if the loop can't converge in a couple of passes or a finding needs a design decision, the escalation back to the conducting `coding-session` with the diagnosis.

## What this skill does NOT do
- Merge, in any mode.
- Modify code in interactive mode without being asked.
- Review the whole codebase instead of the change and its blast radius.
- Manufacture findings, soften a genuine blocker, or thrash on a fix it can't land — escalate instead.
