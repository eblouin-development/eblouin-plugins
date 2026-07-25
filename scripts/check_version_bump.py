#!/usr/bin/env python3
"""
Check that a pull request carries the version bump its release:* label calls for.

The bump lives in the PR itself (not in a bot commit on main), so this is what
keeps it honest: given the version on the base branch, the version on the PR
head, and the PR's release level, it verifies the head is exactly the version
the level implies.

  python scripts/check_version_bump.py --base 1.2.3 --head 1.3.0 --level minor

`--level none` (from a `release:none` label) inverts the check: the version must
NOT have moved. Used for changes that don't ship to consumers.

Exits non-zero with an explanation on any mismatch. See
.claude/skills/library-contribution/SKILL.md for the contract this enforces.
"""
import argparse
import re
import sys

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse(version, what):
    m = SEMVER_RE.match(version.strip())
    if not m:
        sys.exit(f"ERROR: {what} version {version!r} is not a bare X.Y.Z semver.")
    return tuple(int(p) for p in m.groups())


def expected(base, level):
    major, minor, patch = base
    if level == "major":
        return (major + 1, 0, 0)
    if level == "minor":
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def fmt(v):
    return ".".join(str(p) for p in v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="version on the base branch")
    ap.add_argument("--head", required=True, help="version on the PR head")
    ap.add_argument("--level", required=True,
                    choices=("major", "minor", "patch", "none"))
    args = ap.parse_args()

    base = parse(args.base, "base")
    head = parse(args.head, "head")

    if args.level == "none":
        if head != base:
            sys.exit(
                f"ERROR: this PR is labelled release:none but moved the version "
                f"{fmt(base)} -> {fmt(head)}.\n"
                f"Either drop the bump, or label the PR with the level it "
                f"actually needs (release:major|minor|patch)."
            )
        print(f"OK: release:none and the version stayed at {fmt(base)}.")
        return

    want = expected(base, args.level)

    if head == base:
        sys.exit(
            f"ERROR: this PR carries no version bump. Base is {fmt(base)}; a "
            f"release:{args.level} PR must set {fmt(want)} in BOTH "
            f"plugins/dev-lifecycle/.claude-plugin/plugin.json and "
            f".claude-plugin/marketplace.json (metadata.version and the "
            f"dev-lifecycle plugin entry).\n"
            f"If this change ships nothing to consumers, label it release:none "
            f"instead. See .claude/skills/library-contribution/SKILL.md."
        )

    if head != want:
        sys.exit(
            f"ERROR: version bump doesn't match the release:{args.level} label.\n"
            f"  base branch: {fmt(base)}\n"
            f"  this PR:     {fmt(head)}\n"
            f"  expected:    {fmt(want)}\n"
            f"If the base moved under you, rebase and recompute the bump from "
            f"the NEW base. If the level is wrong, change the label instead of "
            f"the number."
        )

    print(f"OK: release:{args.level} bump {fmt(base)} -> {fmt(head)}.")


if __name__ == "__main__":
    main()
