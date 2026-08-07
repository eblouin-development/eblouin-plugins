---
name: "testing"
description: "Design a test strategy and write the test suite — unit, integration, and end-to-end — for backend and frontend code. Use this skill WHENEVER the work involves writing or improving tests: \"write tests for X\", \"add test coverage\", \"set up testing\", \"test this endpoint/component\", \"I want to TDD this\", or when a feature needs tests before it can ship through the pipeline. Default tooling is pytest (Python/FastAPI/Django) and the project's JS runner with Testing Library (frontend) plus Playwright for end-to-end, but it detects and conforms to the project's existing setup. Tests written here are what the devops CI gate runs and the code-review skill checks for — so they must be meaningful, not coverage theater."
---

# Testing

Write tests that catch real regressions and survive refactors. This skill is the keystone of the suite: the devops pipeline *gates* on these tests and code-review *checks for* them, so their value flows through everything else. Tests that assert implementation details, or pad a coverage number without checking behavior, are worse than no tests.

The guiding idea: **test behavior and contracts, not internals.** A good test describes what the code should do from the outside; it keeps passing when you refactor *how* and fails only when *what* breaks.

## Core rules

- **Detect and conform.** Read the project's existing test setup first — runner, layout, fixtures, naming, how the DB and network are handled. Match it; don't add a second framework alongside one that's there.
- **Test behavior, not implementation.** Assert observable outcomes and public contracts, not private internals, call counts, or DOM structure. Query UIs by role/label/text.
- **Respect the pyramid.** Many fast unit tests, a meaningful layer of integration tests, a few high-value e2e tests on critical flows. Don't invert it.
- **Deterministic, isolated, independent.** No order dependence, no shared mutable state, no reliance on wall-clock/network/randomness unless controlled. A flaky test is a broken test.
- **Cover the unhappy paths.** Errors, edge cases, empty/null inputs, validation and auth failures, boundaries.
- **Every test must be shown to fail.** A test that cannot fail is worse than none — it holds the slot a real guard would occupy and reports green forever. Demonstrate red before green: break the thing on purpose (or revert the fix) and confirm this test notices, with the failure message the bug predicts. The procedure, and the ways the check itself silently no-ops, are in `${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md`.
- **Assert the specific thing, not a substring of the page.** Pin the interpolated value belonging to *this* case, not a marker that three other elements also carry. A whole-page or whole-file `in` check passes regardless of what happens to the subject under test.
- **Coverage is a tool, not a target.** Meaningful coverage of logic and risk, not a vanity percentage. The project's threshold is a CI floor, not the goal.
- **Work context-efficiently.** Detect setup from config/manifest; test the changed surface, not the whole tree. See `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`.

## Workflow

### 1. Detect the test setup (always)
Find the runner and libs (`pytest` + `pytest-asyncio`/`httpx`; `vitest`/`jest` + Testing Library; `playwright`), the layout and conventions (where tests live, fixtures/factories, how the test DB and external services are handled), and what's covered vs missing. State what you found and the plan in a line.

### 2. Plan the strategy
Decide what belongs at each level: pure logic → unit; an endpoint through the DB or a component with its data layer → integration; a complete user journey → one e2e. Turn the planning skill's **acceptance criteria** into concrete test cases (they're the ready-made "done and correct" checklist). Identify the boundaries to control: DB, network, time, randomness.

### 3. Write the tests
Load the reference for the layer:
- **Backend** → `${CLAUDE_PLUGIN_ROOT}/references/testing/backend-testing.md`.
- **Frontend** → `${CLAUDE_PLUGIN_ROOT}/references/testing/frontend-testing.md`.

Structure each test Arrange–Act–Assert; one behavior per test; name so a failure reads like a sentence; assert specific values; use factories/fixtures; mock only at the boundary (external network, time, third-party) — never your own internal code.

### 4. TDD (when chosen)
Red → green → refactor: a failing test pinning the behavior, minimum code to pass, then refactor with the test as a net. Offer it for well-specified logic; not mandatory for exploratory/UI-polish work.

TDD gets the red phase for free. **When the test is written after the code — a regression test on an already-applied fix, or coverage added to existing behavior — the red phase has to be manufactured**, and that is exactly when a can't-fail test slips through. Revert the source (never `git stash push <paths>`, which no-ops on a committed tree and produces a false pass) or strip the specific thing being pinned, watch the test fail with the expected message, restore, watch it pass. Record both results. Full procedure: `${CLAUDE_PLUGIN_ROOT}/shared/verification-evidence.md`.

Also confirm the test can execute where it is meant to gate. A suite whose runner or browser isn't installed in CI **skips** rather than fails — green, and guarding nothing.

### 5. Hand off
Summarize what's tested, at which levels, and gaps left (and why). Confirm the suite runs clean and is wired into how the project runs tests so the devops gate picks it up — part of `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`.

## How this works with the other skills
- **planning** acceptance criteria → test cases here. **frontend/backend** build; this hardens. **devops** runs this as the CI gate. **code-review** checks that changed code is tested and tests are meaningful.

## What this skill does NOT do
- Write tests coupled to implementation details that break on every refactor.
- Pad coverage with assertion-free or trivial tests.
- Invert the pyramid. Introduce a second test framework into a project that already has one. Leave flaky/order-dependent tests behind.
