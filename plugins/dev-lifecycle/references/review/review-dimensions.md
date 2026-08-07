<!--
library: code-review
versions-covered: "n/a"
last-verified: 2026-08-07
provenance: manual
sources: []
-->

# Review dimensions (correctness, best practices, DRY, performance)

Detailed criteria for the non-security dimensions. Apply to the changed code and its blast radius. Security has its own file (`security.md`).

The first three classes below are the shapes this firm's reviews actually catch most often, and each is one a careful read of the diff will miss on its own — they are found by enumerating siblings, following a declaration to its consumer, and checking prose against code.

## Correctness & regression — "does it break anything?"
The most important dimension: working code that ships beats elegant code that breaks.

- **Logic:** off-by-one errors, inverted conditions, wrong operators, incorrect boundary handling, mishandled `null`/`None`/`undefined`/empty states.
- **Broken contracts:** if a function signature, return type, or behavior changed, do all callers still work? Did a renamed/removed export break imports? Did a changed API response shape break the frontend consuming it?
- **State & concurrency:** shared mutable state, race conditions, missing `await`, unhandled promise rejections, ordering assumptions that don't hold.
- **Error paths:** are failures handled, or do they throw unhandled and crash/500? Are partial failures left in an inconsistent state (e.g. write A succeeds, write B fails, no rollback)?
- **Tests:** does the change break existing tests? Should it have added or updated tests? Is the changed logic actually covered, or only the happy path?
- **Data flow:** trace inputs through the change to outputs. Does every branch produce a valid result?

## Partial enforcement — the rule on one of N paths
The most common real defect in this firm's review record. A change adds or moves a guard, a
contract, or a convention, and applies it to the path in front of it while sibling paths reach the
same state unguarded.

- **Enumerate the siblings before judging the guard.** For a model-level rule: `save()`,
  `QuerySet.update()`, `bulk_update()`, `bulk_create()`, raw SQL, the admin's change form, any
  service that writes the field. For a verb in a set of verbs: every other verb in that module. For
  a shared partial or component: every sibling that implements the same contract. For a validated
  input: every entry point that accepts it.
- **Compare against the codebase's own precedent.** The strongest form of this finding is
  comparative — *"unlike its sibling `logs()`, which bounds history with `--tail`"*, *"unlike
  `ProductQuerySet.search()`, which scopes to `verified()` internally"*, *"unlike every other
  mutating verb in this file, which wraps acquire/release in `try/finally`."* If the codebase
  already solved this one way, a new path that solves it differently — or not at all — is the
  finding.
- **"No caller does that today" is not a defense.** An unguarded bulk path with no current call
  site is a live gap in a stated guarantee; the next caller is the exploit. Note the severity
  honestly (not yet reachable) without dismissing it.
- **A fix applied to some of its sites is still open.** When an earlier finding's ruling lands on
  one of three partials, the finding has not been resolved (`${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md`).

## Declared but not wired
Something is defined, documented, exported, or tested, and nothing actually reaches it. The feature
appears shipped and silently does nothing — the failure mode that green gates are worst at catching.

- **New setting** → is it in `.env.example` (or the project's equivalent) *and* in the deploy
  surface that must pass it through — the compose `environment:` block, the task definition, the
  chart? Documented-but-not-passed and passed-but-not-documented are both findings.
- **New config value, token, or constant** → is anything referencing it? A design token defined,
  documented and exported to a second platform while every consumer still hardcodes the literal is
  dead weight that reads as adoption.
- **New function, method, or admin action** → does it have a production call site, or only a test?
- **New CI step, gate, or lint rule** → is it in the workflow file the pipeline actually runs, or in
  a staging copy waiting for someone to move it? A gate that isn't wired gates nothing while
  appearing to.
- **New framework-level override** → does the framework honor it where it's set? A class attribute
  the base class's `__init__` assigns unconditionally is inert, and nothing about the diff shows it
  (`${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md`).

## Claim vs. code — prose the implementation doesn't back
Docstrings, code comments, `CLAUDE.md` lines, PR bodies, and user-facing safety copy are in scope.
They are read as guarantees by the next person, and they drift silently because nothing tests them.

- **Verify the claim, don't read past it.** A docstring promising an output cap that no code
  enforces; a `CLAUDE.md` line citing a test file that never touches the subject; safety copy telling
  the operator that values "reset on the next redeploy" when the deploy path never force-recreates.
- **Wrong reasoning behind a correct conclusion is still a finding.** A comment asserting a state is
  safe "by construction" on an argument that doesn't hold invites the next change to rely on it. The
  observed case: a pressed button state documented as contrast-safe because the fill darkens —
  ignoring that the filter darkens the white label too, so the real ratio *fell*.
- **Comments describing a world that has moved.** A build-time exclusion justified by *"nothing in
  the running app reads this at runtime"* — true when written, false ten commits later once a new
  view read it at request time. True-when-written comments are a standing review target on any
  multi-step change.
- **PR-body claims count.** Names, file paths, and "this keeps resolving" assurances in the
  description get checked against the diff like anything else.

## Best practices & conventions
"Best practice" = idiomatic for this stack/version AND consistent with this codebase. Defer to the project; the `frontend` and `backend` skills define the substantive standards.

- **Version-correct idioms:** patterns valid for the installed React / Pydantic / SQLAlchemy version (see those skills). Flag legacy patterns where a modern one applies, and modern APIs used on a version that lacks them.
- **Consistency:** matches existing naming, file/folder structure, error-handling style, and patterns. A change that's "good" but alien to the codebase still adds friction.
- **Separation of concerns:** business logic out of route handlers and components; presentation separate from data access; single-responsibility units.
- **Readability:** clear names, reasonable function size, no dead code, no leftover debug prints/`console.log`, no commented-out blocks shipped.
- **Typing:** honest types, no `any`/`# type: ignore` as an escape hatch, no silenced linters hiding real issues.
- **Magic values & config:** constants named, configuration not hardcoded.

## DRY (Don't Repeat Yourself)
- Logic duplicated by this change — the same computation, validation, or transform written more than once where one source of truth would do.
- Copy-paste that drifts: near-identical blocks that will need to be fixed in multiple places.
- Reinvented wheels: hand-rolled logic that duplicates an existing project utility or a well-known library function.
- **Don't over-correct:** incidental similarity is not duplication. Two things that look alike today but change for different reasons should often stay separate. Premature abstraction is its own cost — flag genuine, meaningful repetition, not every echo.

## Performance & scalability
Focus on what degrades as data or traffic grows, not micro-optimizations.

- **N+1 queries:** a query inside a loop over rows; relationships lazy-loaded per item. The classic backend performance bug — flag it and suggest eager loading / a batched query.
- **Unbounded work:** list endpoints/queries with no pagination or limit; loading an entire table to compute something the DB could; rendering an unvirtualized huge list on the frontend.
- **Algorithmic complexity:** accidental O(n²) (nested loops over the same growing collection), repeated work that could be hoisted/memoized, expensive operations in hot paths.
- **Database:** new frequent query/filter/sort/join patterns without a supporting index; `SELECT *` pulling unused columns; missing query limits.
- **Async hygiene:** blocking I/O on an async path stalling the event loop; sequential awaits that could run concurrently; unnecessary round-trips.
- **Frontend:** avoidable re-renders, unmemoized expensive work where the React Compiler isn't handling it, large bundle additions, unbatched network requests.
- **Caveat:** don't demand optimization without cause. Note the cost and when it'll bite ("fine now; will be slow past ~10k rows"), and prioritize accordingly.
