#!/usr/bin/env python3
# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Check that this branch is consistent about which Odoo release it targets.

One branch per supported release is Odoo's convention, and the two branches here
differ in a handful of deliberate places. This checks that the handful stays a
handful and that none of it is half-applied — the failure mode a port produces.

The dangerous one is the table constraint, and it is the reason this script
exists. Odoo 19 declares constraints as ``models.Constraint``; 18 uses
``_sql_constraints``. Get it wrong towards 18 and Odoo says so at once. Get it
wrong towards 19 and Odoo accepts the old attribute, logs one warning, and then
**ignores it** — the constraint is simply never created. Two contacts share a
GLN, the same movement enters the outbox twice, two pallets carry one SSCC, and
nothing fails. No test can miss a rule that does not exist, which is why this is
a lint and not a test.

Nothing here is configured per branch. The target release is read from the
addon manifests, so the same file stands unchanged on both branches and cannot
drift out of step with the code it guards.

    python tools/check-release-idioms.py

Exit status is 0 when the branch agrees with itself, 1 when it does not.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Where the release shows up outside the manifests. A port that forgets one of
# these builds or tests against the wrong Odoo without saying anything.
IMAGE_FILES = (
    ".github/workflows/ci.yml",
    "docker-compose.yml",
    "docker/demo/Containerfile",
)
IMAGE_RE = re.compile(r"odoo:(\d+)")
VERSION_RE = re.compile(r'"version"\s*:\s*"(\d+)\.0\.')


def manifests():
    return sorted(ROOT.glob("openepcis_connector*/__manifest__.py"))


def target_release(problems):
    """The Odoo major version every manifest claims, or None if they disagree."""
    found = {}
    for path in manifests():
        match = VERSION_RE.search(path.read_text(encoding="utf-8"))
        if not match:
            problems.append("%s: no version of the form <release>.0.x.y.z" % rel(path))
            continue
        found.setdefault(match.group(1), []).append(rel(path))
    if not found:
        problems.append("no addon manifest carries a version — cannot tell the release")
        return None
    if len(found) > 1:
        # A half-finished port: some addons moved, some did not. Odoo would
        # refuse to install the stragglers, but only once somebody tries.
        for release, paths in sorted(found.items()):
            problems.append("manifests claim Odoo %s: %s" % (release, ", ".join(paths)))
        return None
    return int(next(iter(found)))


def check_constraints(release, problems):
    """The idiom that decides whether a uniqueness rule exists at all."""
    if release >= 19:
        forbidden, expected = "_sql_constraints", "models.Constraint"
        complaint = "Odoo %d accepts it, warns once, then ignores it — no rule is created"
    else:
        forbidden, expected = "models.Constraint", "_sql_constraints"
        complaint = "does not exist on Odoo %d"
    for path in sorted(ROOT.glob("openepcis_connector*/**/*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # A mention in a comment is how the branches explain themselves to
            # each other; only a declaration counts.
            if line.lstrip().startswith("#"):
                continue
            if forbidden in line:
                problems.append(
                    "%s:%d: %s — %s. Use %s."
                    % (rel(path), number, forbidden, complaint % release, expected)
                )


def check_images(release, problems):
    """The container tags have to name the release the manifests claim."""
    for name in IMAGE_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for tag in IMAGE_RE.findall(line):
                if int(tag) != release:
                    problems.append(
                        "%s:%d: builds against odoo:%s, but the addons declare Odoo %d"
                        % (name, number, tag, release)
                    )


def rel(path):
    return str(path.relative_to(ROOT))


def main():
    problems = []
    release = target_release(problems)
    if release is not None:
        # Flushed, so the release line stays above the problems it explains
        # rather than being buffered past them in CI output.
        print("This branch targets Odoo %d." % release, flush=True)
        check_constraints(release, problems)
        check_images(release, problems)
    for problem in problems:
        print("  %s" % problem, file=sys.stderr)
    if problems:
        print("\n%d problem(s)." % len(problems), file=sys.stderr)
        return 1
    print("Manifests, constraint idiom and container tags agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
