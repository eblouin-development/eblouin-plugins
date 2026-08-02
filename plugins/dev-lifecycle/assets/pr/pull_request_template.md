<!--
Shippable PR template — copied by `scaffolding` into a firm project repo's
`.github/pull_request_template.md`. Generic by design: it reflects the firm's
shared definition-of-done (see plugins/dev-lifecycle/shared/definition-of-done.md),
not this plugin repo's own conventions. Keep it lean — fill what applies.
-->

## Summary
One or two sentences: what this PR does and why.

Closes #

## What changed
-

## Testing
What was run, and the results (unit/integration/e2e, manual verification). A
fixed bug should point at its regression test.

CI gates run on ready-for-review PRs only, so while this was a draft the checks
ran in the container — say which ones, and name anything the container couldn't
run (a job needing a secret, a service, or a runner matrix) that the CI run
started by the flip is the first to cover.

## Security
Security-relevant considerations this PR touches (auth/authorization on
protected routes, input validation, secrets, dependency changes) and how the
secure baseline was met — or "none" if this PR has no security surface.

## Docs updated
- [ ] The co-located doc for whatever this PR touches (block/component README,
      module doc, or equivalent) is current
- [ ] The root `README.md` section this belongs in is current
- [ ] An ADR was added/updated if this PR makes a significant decision worth recording

## Decision log
Judgment calls made while building this — anything a reviewer should
sanity-check rather than assume — or "none" if there weren't any.

---
<!-- scaffolding substitutes the actual repo owner's GitHub handle here on copy -->
cc @<owner>
