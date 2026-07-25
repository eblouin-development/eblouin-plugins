#!/usr/bin/env python3
"""
Validate the dev-lifecycle plugin before it ships.

Deterministic, no-auth structural checks that mirror what Claude Code rejects on
install — the JSON manifests, and (the one that bit us) the YAML frontmatter of
every SKILL.md. Run locally with `python scripts/validate_plugin.py`; runs in CI
on every push and PR. Exits non-zero on any error.
"""
import json
import os
import re
import sys
import glob

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required (pip install pyyaml)")
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "dev-lifecycle")
ALLOWED_PLUGIN_FIELDS = {
    "name", "version", "description", "author",
    "homepage", "repository", "license", "keywords",
}

errors = []
warnings = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        err(f"missing file: {os.path.relpath(path, ROOT)}")
    except json.JSONDecodeError as e:
        err(f"invalid JSON in {os.path.relpath(path, ROOT)}: {e}")
    return None


# 1. marketplace.json
mkt = load_json(os.path.join(ROOT, ".claude-plugin", "marketplace.json"))
if isinstance(mkt, dict):
    if not mkt.get("name"):
        err("marketplace.json: missing 'name'")
    if not isinstance(mkt.get("owner"), dict):
        err("marketplace.json: 'owner' must be an object")
    plugins = mkt.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        err("marketplace.json: 'plugins' must be a non-empty array")
    else:
        has_root = bool(mkt.get("metadata", {}).get("pluginRoot"))
        for i, p in enumerate(plugins):
            if not p.get("name"):
                err(f"marketplace.json: plugins[{i}] missing 'name'")
            if not p.get("source") and not has_root:
                err(f"marketplace.json: plugins[{i}] missing 'source' "
                    "(and no metadata.pluginRoot set)")

# 2. plugin.json
pj = load_json(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"))
if isinstance(pj, dict):
    for req in ("name", "version", "description"):
        if not pj.get(req):
            err(f"plugin.json: missing required field '{req}'")
    extra = set(pj) - ALLOWED_PLUGIN_FIELDS
    if extra:
        err(f"plugin.json: unsupported field(s) {sorted(extra)} "
            f"(allowed: {sorted(ALLOWED_PLUGIN_FIELDS)})")
    author = pj.get("author")
    if author is not None and not (isinstance(author, dict) and author.get("name")):
        err("plugin.json: 'author' must be an object with a 'name'")

# 2b. the three version fields must agree. The bump is written by hand in the
#     PR (see .claude/skills/library-contribution/SKILL.md), so a half-applied
#     bump — one file moved, the other not — is the likely mistake, and it ships
#     a marketplace whose advertised version isn't what installs.
if isinstance(pj, dict) and isinstance(mkt, dict):
    pj_version = pj.get("version")
    mkt_meta = mkt.get("metadata", {}).get("version")
    entries = [p for p in (mkt.get("plugins") or []) if p.get("name") == pj.get("name")]
    mkt_entry = entries[0].get("version") if entries else None
    seen = {
        "plugin.json version": pj_version,
        "marketplace.json metadata.version": mkt_meta,
        f"marketplace.json plugins[{pj.get('name')}].version": mkt_entry,
    }
    if len({v for v in seen.values() if v is not None}) > 1:
        err("version mismatch — all three must agree: "
            + ", ".join(f"{k}={v!r}" for k, v in seen.items()))

# 3. every SKILL.md frontmatter must be valid YAML with name + description
skills = sorted(glob.glob(os.path.join(PLUGIN, "skills", "*", "SKILL.md")))
if not skills:
    err("no skills found under plugins/dev-lifecycle/skills/")
for path in skills:
    rel = os.path.relpath(path, ROOT)
    skill_dir = os.path.basename(os.path.dirname(path))
    text = open(path).read()
    if not text.startswith("---"):
        err(f"{rel}: missing YAML frontmatter")
        continue
    parts = text.split("---", 2)
    if len(parts) < 3:
        err(f"{rel}: malformed frontmatter (no closing '---')")
        continue
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        err(f"{rel}: invalid YAML frontmatter -> {e}")
        continue
    if not isinstance(data, dict):
        err(f"{rel}: frontmatter is not a mapping")
        continue
    name = data.get("name")
    if not name:
        err(f"{rel}: frontmatter missing 'name'")
    elif name != skill_dir:
        err(f"{rel}: name '{name}' does not match directory '{skill_dir}'")
    if not data.get("description"):
        err(f"{rel}: frontmatter missing 'description'")

skill_names = {os.path.basename(os.path.dirname(p)) for p in skills}

# 4. every ${CLAUDE_PLUGIN_ROOT}/... path referenced in skill bodies, shared
#    docs, and references must resolve to a real file/dir under the plugin
#    root — this is the hard-error version of what docs/SETUP-AND-USAGE.md
#    used to self-declare "unverified". Deleting or renaming a referenced
#    reference/shared-doc/template/asset now fails the gate.
#
#    Matching rule: capture the path segment after `${CLAUDE_PLUGIN_ROOT}/`
#    up to the first character outside `[A-Za-z0-9_./-]` (backticks, spaces,
#    sentence punctuation all fall outside that set, so no explicit
#    "stop at backtick" logic is needed — it's implicit in the charset).
#    A trailing "." or "/" picked up from a path that closes a sentence
#    (e.g. "...at ${CLAUDE_PLUGIN_ROOT}/templates/monorepo/.") is stripped
#    before resolving. The capture charset already excludes "<", ">", and
#    ellipsis, so illustrative placeholders (e.g.
#    `${CLAUDE_PLUGIN_ROOT}/templates/<layer>/<name>`) never match in the
#    first place.
CLAUDE_PLUGIN_ROOT_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+)")
ROOT_REF_GLOBS = [
    os.path.join(PLUGIN, "skills", "**", "*.md"),
    os.path.join(PLUGIN, "shared", "**", "*.md"),
    os.path.join(PLUGIN, "references", "**", "*.md"),
    os.path.join(PLUGIN, "templates", "**", "*.md"),
]
seen_root_refs = set()
for pattern in ROOT_REF_GLOBS:
    for path in glob.glob(pattern, recursive=True):
        rel = os.path.relpath(path, ROOT)
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for m in CLAUDE_PLUGIN_ROOT_RE.finditer(text):
            target = m.group(1).rstrip(".").rstrip("/")
            if not target:
                continue
            key = (path, target)
            if key in seen_root_refs:
                continue
            seen_root_refs.add(key)
            if not os.path.exists(os.path.join(PLUGIN, target)):
                err(f"{rel}: dangling reference "
                    f"'${{CLAUDE_PLUGIN_ROOT}}/{target}' — target does not exist")

# 5. skill-name mentions in skill bodies must resolve to a real skill
#    directory, catching the `web-proposal-writer`-style dead link before it
#    ships. Matching rule (deliberately narrow, to avoid flagging ordinary
#    prose words that happen to sit in backticks — CLI flags, package names,
#    model names, etc.):
#      (a) a backtick-quoted lowercase-kebab token immediately followed by
#          the word "skill"/"skills" (optionally through a possessive "'s"),
#          e.g. `` `design-system` skill `` or `` `frontend`'s skill ``.
#      (b) the object of explicit handoff phrasing: "hand off to `X`" /
#          "handoff to `X`".
#    Anything else backticked is left alone — this is a precision-first
#    heuristic; it will not catch every prose reference to a skill; that's
#    an accepted false-negative tradeoff to keep it from crying wolf.
#    A small allowlist covers skills this plugin refers to that are shipped
#    by the base Claude Code install / another plugin, not this one.
EXTERNAL_SKILLS = {"docx", "pdf", "pptx", "xlsx", "humanizer", "ruthless-edit"}
SKILL_MENTION_RES = [
    re.compile(r"`([a-z][a-z0-9-]*)`(?:'s)?\s+skills?\b"),
    re.compile(r"\b(?:hand off to|handoff to)\s+`([a-z][a-z0-9-]*)`"),
]
for path in skills:
    rel = os.path.relpath(path, ROOT)
    text = open(path, encoding="utf-8").read()
    parts = text.split("---", 2)
    body = parts[2] if len(parts) >= 3 else text
    mentioned = set()
    for pattern in SKILL_MENTION_RES:
        for m in pattern.finditer(body):
            mentioned.add(m.group(1))
    for name in sorted(mentioned):
        if name not in skill_names and name not in EXTERNAL_SKILLS:
            err(f"{rel}: references nonexistent skill '{name}' "
                f"(no plugins/dev-lifecycle/skills/{name}/, and not in the "
                "known external-skill allowlist)")

# 6. every template block / catalog component ships its docs/fragment.md
#    (the documentation standard's "ships its doc" acceptance bar), and any
#    `versions-pinned-to:` pointer in its README resolves to a real file.
#    Block/component roots are identified by known catalog anchor points,
#    NOT by scanning every README.md under templates/ recursively — that
#    would wrongly demand a fragment from implementation-internal
#    sub-modules (e.g. templates/infra/aws-fargate/modules/ecr, a Terraform
#    submodule of the aws-fargate block with its own README but no
#    independent catalog entry) and from templates/monorepo (the
#    scaffolding skeleton, whose own README.md doubles as its fragment per
#    its frontmatter's `exposes` list — confirmed by reading it).
#    `versions-pinned-to` values are a fixed pointer resolved against the
#    plugin root, not the block's own directory — every block at every
#    nesting depth writes the same unqualified `references/compatibility-
#    matrix.md`, never a `../../..`-relative path (see templates/_TEMPLATE-
#    README.md, the authoring spec for this field).
BLOCK_ROOT_GLOBS = [
    os.path.join(PLUGIN, "templates", "backend", "*"),
    os.path.join(PLUGIN, "templates", "frontend", "*"),
    os.path.join(PLUGIN, "templates", "mobile", "*"),
    os.path.join(PLUGIN, "templates", "infra", "*"),
    os.path.join(PLUGIN, "templates", "worker", "*"),
    os.path.join(PLUGIN, "templates", "packages", "*"),
    os.path.join(PLUGIN, "templates", "components", "*"),
    os.path.join(PLUGIN, "templates", "components", "*", "*"),
]
VERSIONS_PINNED_RE = re.compile(r"^versions-pinned-to:\s*(\S+)", re.MULTILINE)
block_roots = set()
for pattern in BLOCK_ROOT_GLOBS:
    for d in glob.glob(pattern):
        if os.path.isfile(os.path.join(d, "README.md")):
            block_roots.add(d)
for d in sorted(block_roots):
    rel_dir = os.path.relpath(d, ROOT)
    if not os.path.isfile(os.path.join(d, "docs", "fragment.md")):
        err(f"{rel_dir}: block/component is missing its docs/fragment.md")
    readme_path = os.path.join(d, "README.md")
    readme_text = open(readme_path, encoding="utf-8").read()
    m = VERSIONS_PINNED_RE.search(readme_text)
    if m:
        pin_target = m.group(1)
        if not os.path.isfile(os.path.join(PLUGIN, pin_target)):
            err(f"{rel_dir}/README.md: versions-pinned-to target "
                f"'{pin_target}' does not exist")

# 7. references and template blocks should carry a `last-verified` metadata
#    header for the freshness audit. Read the WHOLE file rather than a
#    byte-capped head — a fixed byte cap silently misses a header that sits
#    lower in a longer file (e.g. behind a long `needs`/`exposes` list),
#    which is exactly the false-negative class this hardening pass closes.
#    Missing `last-verified` is promoted from warning to ERROR for any file
#    whose header declares `provenance: manual` — a manually-authored file
#    has no automated process to fall back on for freshness, so an absent
#    header there is a real gap, not just a nice-to-have. Auto-generated
#    provenance (or no provenance field at all) stays a warning: those files
#    may be regenerated by tooling that doesn't yet stamp the header, and
#    flagging them as a hard gate would be premature until that's true of
#    every generator (none of the current repo's files hit this warning
#    path today, but the distinction is kept lenient on purpose).
#    `_`-prefixed files are schema exemplars (e.g. _TEMPLATE-README.md) and
#    are skipped. `docs/fragment.md` files are skipped: the canonical
#    fragment format is machine-consumed (a single `<!-- fragment: ... -->`
#    header line, no metadata slot — see references/authoring/
#    documentation-standard.md); freshness for a block/component is carried
#    by its README, which does carry `last-verified`.
PROVENANCE_MANUAL_RE = re.compile(r"^provenance:\s*manual\b", re.MULTILINE)


def check_last_verified(path):
    rel = os.path.relpath(path, ROOT)
    try:
        text = open(path, encoding="utf-8").read()
    except (UnicodeDecodeError, OSError):
        return
    if "last-verified:" in text:
        return
    if PROVENANCE_MANUAL_RE.search(text):
        err(f"{rel}: no 'last-verified' metadata header "
            "('provenance: manual' requires one)")
    else:
        warn(f"{rel}: no 'last-verified' metadata header")


for path in glob.glob(os.path.join(PLUGIN, "references", "**", "*.md"), recursive=True):
    if os.path.basename(path).startswith("_"):
        continue
    check_last_verified(path)

for path in glob.glob(os.path.join(PLUGIN, "templates", "**", "*.md"), recursive=True):
    if os.path.basename(path).startswith("_"):
        continue
    if path.endswith(os.path.join("docs", "fragment.md")):
        continue
    check_last_verified(path)

# 8. no hardcoded personal handle in SHARED skill/workflow text. This repo's
#    own CLAUDE.md and .github/pull_request_template.md are allowed to keep a
#    literal `cc @<handle>` convention (it's genuinely this repo's owner and
#    the README documents the substitution on rename); everything that ships
#    to — or is read by — every other repo must not hardcode one owner.
PERSONAL_HANDLE = "eblouin876"
HANDLE_EXEMPT = {
    os.path.join(ROOT, "CLAUDE.md"),
    os.path.join(ROOT, ".github", "pull_request_template.md"),
}
HANDLE_SCAN_GLOBS = [
    os.path.join(PLUGIN, "skills", "**", "*.md"),
    os.path.join(PLUGIN, "assets", "**", "*"),
    os.path.join(ROOT, ".github", "workflows", "*.yml"),
    os.path.join(ROOT, ".github", "workflows", "*.yaml"),
]
for pattern in HANDLE_SCAN_GLOBS:
    for path in glob.glob(pattern, recursive=True):
        if not os.path.isfile(path) or path in HANDLE_EXEMPT:
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        if "@" + PERSONAL_HANDLE in text:
            err(f"{os.path.relpath(path, ROOT)}: hardcodes the personal handle "
                f"'@{PERSONAL_HANDLE}' — shared skill/workflow text must use the "
                "'<owner>' placeholder (or, in a workflow that can, derive it "
                "with '${{ github.repository_owner }}') instead of a literal "
                "personal handle")

# report
for w in warnings:
    print(f"::warning:: {w}" if os.getenv("GITHUB_ACTIONS") else f"WARN  {w}")
if errors:
    print()
    for e in errors:
        print(f"::error:: {e}" if os.getenv("GITHUB_ACTIONS") else f"ERROR {e}")
    print(f"\nValidation FAILED: {len(errors)} error(s), {len(warnings)} warning(s).")
    sys.exit(1)

print(f"Validation passed: {len(skills)} skills, {len(warnings)} warning(s).")
