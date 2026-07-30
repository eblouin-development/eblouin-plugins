# Context & token efficiency

The canonical efficiency reference. Every skill points here; the highest-value rules also appear as a core rule in each skill's SKILL.md. Read this when you need the fuller playbook (e.g. setting up a project's persistent context, or deciding whether to spawn a subagent).

Governing idea: **context is the budget, and the repository is the memory.** Every token spent reading, re-reading, or dumping code is a token not spent thinking — and a direct cost. Work from the smallest slice of context that lets you act correctly, and let durable artifacts (issues, PRs, API contracts, ADRs, a lean CLAUDE.md) carry state so the conversation doesn't have to.

## Retrieval discipline (the biggest lever)
- **Locate before you read.** Use grep/glob/symbol search to find the exact file and span, then read only that span (line ranges) — not whole files, and never whole directories "to orient yourself."
- **Read for a reason.** Open a file only when its contents will change what you do. If a fact is reasonably inferable, state it as an assumption rather than verifying it exhaustively.
- **Detect from manifests, not source.** Stack/version detection reads `package.json`, `pyproject.toml`, lockfiles — small and high-signal — not application code.
- **Don't re-read.** Track what you've already seen; re-opening a file you read earlier is pure waste.

## Progressive disclosure
- Load only the reference file(s) the task needs (the React path *or* the HTMX path, not both). Never load a skill's whole `references/` set defensively.
- Keep the always-on surface small: skill descriptions sit in context for every request, so they stay tight.

## Scope to the change
- Work the minimal relevant surface — the diff and its blast radius, not the whole codebase. Review, tests, and fixes target what changed and what that change can break.

## Distill at handoffs — the repo is the memory
- State flows between steps through **durable artifacts**, not raw transcript: the plan lives in the issue, the change in the PR, the API shape in the contract, a decision in an ADR.
- Hand off the distilled artifact (the contract, the acceptance criteria, the diagnosis) — not the exploration that produced it. This is what stops a long pipeline from accumulating context linearly.
- The same rule applies to a session handing off **to itself** when its window fills: flush state to the issue and PR, write a continuation brief, reload the skills in play, and resume — see `${CLAUDE_PLUGIN_ROOT}/shared/context-continuity.md`.

## Isolate heavy exploration in subagents
- A bounded investigation (trace a bug, survey how a pattern is used across the repo, research an approach) runs in a **subagent** with its own context window and returns a short result. The main thread gets the conclusion, not the search.
- Natural fits: the review step, debugging investigation, and any "go find out X" that would otherwise flood the main context.
- Independent investigations run **concurrently**, dispatched in one message — read-only work can't collide, so serializing it just costs wall-clock time.
- See also: `${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md` for how to watch the subagents this rule tells you to spawn — active, backstop-only cadence rather than polling — and `${CLAUDE_PLUGIN_ROOT}/shared/parallelization.md` for deciding which of them may run at the same time.

## Persistent project context (the biggest recurring saver)
- A lean, high-signal **CLAUDE.md** per repo caches the stable facts every skill would otherwise re-derive: stack and versions, project layout, the commands to run tests/lint/build, and key conventions. This stops every task from re-paying detection.
- Keep it lean — it's always loaded. Stable, high-value facts only; not a changelog, not everything. `scaffolding` generates it; keep it current as conventions change.

## Output discipline
- Prefer diffs/patches and a few-line change summary over reprinting whole files. Don't echo back code the user can already see in the PR.
- Explain what changed and why it matters — not line-by-line narration.

## Model routing (pipeline level)
- Not every step needs the most capable model. Planning and review benefit from a stronger model; mechanical steps can run a cheaper one.
- **In the GitHub Action**, set the model per job via `claude_args` (`--model ...`) — a cost lever configured at the pipeline, not inside each skill.
- **In a `coding-session`** (local subagents), route per spawn with the `Agent` tool's `model` parameter: reasoning/judgment stages (planning, plan-review, code-review) on `opus`, build/implementation on `sonnet`. An unset model inherits the orchestrator's — the most expensive default — so set it explicitly on every spawn. See the coding-session skill's "Model routing" table.
