# Plan ↔ open-issue cross-check

The canonical reference for checking a freshly generated plan against what the repo already has open. Any skill that produces a plan a human is about to approve points here.

Governing idea: **a plan is written against the codebase, but it lands in a repo that already has a backlog.** The plan's author sees the code; it doesn't see that issue #42 asked for this exact thing three weeks ago, that #58 is mid-flight in a PR touching the same module, or that #61 recorded a decision this plan quietly reverses. Those collisions are cheap to find before approval and expensive to find after merge — so every generated plan gets cross-checked against the repo's **open** issues and PRs, and the result is presented at the approval gate along with the plan.

The check is mechanical, read-only pattern-matching against a backlog: it runs in a **`sonnet` subagent**, concurrently with whatever else is verifying the plan, and never blocks drafting.

## Running the check

Brief the subagent with: the plan (goal, approach, step list, and the files/modules it touches), the repo, the epic or milestone this sits under if any, and this file. Tell it to search **open** issues *and* open pull requests — a PR is where an actual in-flight collision lives — by keywords from the goal and steps, by the paths and modules the plan touches, and by the relevant labels, milestone, or epic.

Every hit lands in exactly one bucket:

- **Closes** — the plan, as written, fully delivers what the issue asks. Candidate for a `Closes #n` link.
- **Addresses (partial)** — real overlap, but the issue survives the plan. Say what would be left.
- **Conflicts** — the plan contradicts it: reverses a decision recorded there, changes a contract another open issue or PR depends on, or edits files an open PR is currently rewriting.
- **Duplicate** — this work is already filed. The plan may not need to exist, or belongs on that issue instead of a new one.

Anything that's merely topically related gets **dropped**. A list of loosely-related issues is noise that trains the reader to skip the section.

Report back compactly, most consequential first: `#n — title — bucket — one line on why — recommended action`. Cap the list (about ten) and say what was cut. If nothing intersects, say so explicitly — "no open issues intersect this plan" is a real result, not an empty one.

## Folding it back in

- Add a **`## Related issues`** section to the plan listing the surviving hits with their bucket and the recommended action, or "None found." It travels with the plan onto the filed issue.
- **Closes** hits → the issue this plan files carries `Closes #n` for them, or the plan is filed against the existing issue rather than a new one.
- **Duplicate** hits → raise it with the user before filing anything. Two issues for one piece of work is the outcome this check exists to prevent.
- **Conflicts** → surface at the approval gate as an explicit decision for the user. Never resolve a conflict with another open issue unilaterally: the other issue may represent a commitment this session can't see.
- **The check never writes.** It does not close, comment on, relabel, or edit another issue, and it does not open one. Acting on its findings happens after the user approves the plan.
- **No GitHub, or no issues** → one line saying so, and move on. A repo without a backlog isn't an error condition.

## See also

`${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md` — why this runs in a subagent: a backlog sweep is exactly the bounded investigation that should return a conclusion, not a search.

`${CLAUDE_PLUGIN_ROOT}/shared/parallelization.md` — it's read-only, so it runs concurrently with the other plan-verification passes rather than after them.
