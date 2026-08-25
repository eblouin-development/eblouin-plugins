# Code comments

The shared rule for every skill that writes or edits source code. Comments describe the code **as
it stands** — what it does now and why it is written the way it is now. The reader is someone
opening the file fresh, months from now, with no access to the conversation, the diff, the plan, or
the review that produced it. The narrative of *change* — what was there before, why it was
replaced, who asked for it — lives in git history, the PR, and the issue. It never lives in the
source.

This doctrine exists because the opposite keeps happening: sessions that edit code leave a trail of
"previously this used X", "changed to handle Y", "removed the old validation" comments. Each one
was true the moment it was written and is confusing forever after — it describes a version of the
file the reader cannot see, and it rots into an outright lie as the code keeps moving.

## Never narrate the change

- **Forbidden shapes** — any comment whose meaning depends on an earlier version of the code or on
  the editing process: "previously…", "this used to…", "changed from X to Y", "now uses…",
  "no longer…", "renamed/moved from…", "added to fix…", "updated per review", "as requested",
  "per the plan", "temporary until the old path is removed" (when nothing tracks that removal).
- **The test:** would this comment make sense to a reader who has never seen any earlier version of
  this file? If it's only meaningful relative to a previous state or to the conversation that
  produced the edit, it is change narration — delete it, or rewrite it as a statement about the
  present ("handles Y" instead of "changed to handle Y").
- **A real, current constraint is not history.** "Kept for compatibility with clients on API v1"
  states a live constraint and belongs; "kept from the old implementation" states history and
  doesn't. If the constraint is real, name the constraint — not the change that ran into it.
- **Clean as you touch.** When an edit lands next to an existing change-narration or stale comment,
  fix that comment in the same commit — don't extend the trail. (Code you aren't touching is out of
  scope; a repo-wide comment sweep is its own task, not a side effect of a feature.)
- **Don't ship commented-out code.** Version control already remembers it.

## Comments earn their place by explaining the present

Comments are useful — this doctrine bans a *kind* of comment, not comments. What a good one does:

- **Unusual code gets a why.** If something is done in a non-obvious way — a workaround, an
  ordering constraint, a performance trick, an obvious approach that doesn't work here — a comment
  says why the code is written that way, as a present-tense fact about the world ("the vendor API
  returns 200 on auth failures, so we check the body"), with a link to the issue/spec where one
  exists. Surprising code with no explanation is a defect, the mirror image of the noise above.
- **Obvious code gets nothing.** A comment that restates what the code plainly shows
  (`# increment i`) is noise; prefer clear names and small functions over compensating comments.

## Every function, method, and class carries a doc comment

- Every function, method, and class (and any non-trivial module) gets a doc comment in the
  project's established style (docstring, JSDoc, etc.): what it does, how to use it, the shape of
  the data in and out — parameters, return value, errors/edge behavior, side effects. Types carry
  type information; the doc comment carries meaning, units, constraints, and behavior.
- **Scale depth to the surface.** A trivial private helper gets one honest line; a public API,
  a class others will subclass, or anything with a non-obvious contract gets the full treatment.
  Coverage is not optional; padding is not the goal.
- Per-language mechanics (docstring styles, JSDoc/TS, generated API docs):
  `${CLAUDE_PLUGIN_ROOT}/references/docs/code-and-api-docs.md`.

## Upkeep is part of every change

- **Changing code means re-reading its comments.** Every edit re-reads the comments and doc
  comments attached to the code it touches — and any it makes stale elsewhere (a docstring
  describing a parameter that just changed shape, a why-comment whose reason no longer holds) — and
  updates or deletes them in the same commit. A comment that no longer matches the code is a bug
  shipped, not a docs nit.
- **Review enforces this.** Change-narration comments, missing doc comments on new
  functions/classes, and comments the change made false are review findings
  (`${CLAUDE_PLUGIN_ROOT}/references/review/review-dimensions.md`, "Claim vs. code").

## See also

`${CLAUDE_PLUGIN_ROOT}/references/docs/code-and-api-docs.md` — per-language docstring/JSDoc/API-doc
mechanics this doctrine sets the policy for.

`${CLAUDE_PLUGIN_ROOT}/references/review/review-dimensions.md` — the review dimensions that treat
violations here as findings.

`${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md` — the merge-ready bar this feeds ("docs moved
with the code").
