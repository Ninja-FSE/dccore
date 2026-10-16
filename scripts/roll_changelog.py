#!/usr/bin/env python3
"""Roll the changelog fragments in docs/changes/ into a release (#1307).

Every PR used to add its entry at the top of `## Unreleased` in
docs/UPDATES.md, and often a bullet in docs/UPDATES-PUBLIC.md. Every entry
went on the same lines, so any two PRs open at the same time conflicted
there, whatever their code - on 2026-10-11 one PR was rebased five times for
it. A PR now writes its entry to a file of its own instead:

    docs/changes/<issue>.md          the internal entry: a `### ` section
    docs/changes/<issue>.public.md   the public bullet, when there is one

and two PRs never touch the same file. docs/changes/README.md has the rules.

At release time:

    python scripts/roll_changelog.py v1.17.0 "The Title"    [--date 2026-10-12]
    python scripts/roll_changelog.py --check                 (validate only)

writes the version heading into both changelogs with the entries under it,
newest issue first, and deletes the fragments. It changes nothing when any
fragment is malformed, and says which and why.
"""

import argparse
import datetime
import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGES_DIR = os.path.join("docs", "changes")
INTERNAL = os.path.join("docs", "UPDATES.md")
PUBLIC = os.path.join("docs", "UPDATES-PUBLIC.md")

# <issue>[-suffix].md or <issue>[-suffix].public.md. The suffix tells apart
# two entries for one issue (a phase 1 and a phase 2).
FRAGMENT_NAME = re.compile(r"^(?P<number>\d+)(?:-(?P<suffix>[a-z0-9][a-z0-9-]*))?(?P<public>\.public)?\.md$")
VERSION = re.compile(r"^v\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?$")
CONFLICT_MARKER = re.compile(r"^(<<<<<<<|=======|>>>>>>>)( |$)", re.M)


class Fragment:
    def __init__(self, path, number, suffix, public, text):
        self.path, self.number, self.suffix, self.public, self.text = path, number, suffix, public, text

    @property
    def order(self):
        """Newest first: the higher issue, then the later suffix of one issue."""
        return (self.number, self.suffix)


def read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read().replace("\r\n", "\n")


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def fragment_problem(name, text, public):
    """Why this fragment cannot be rolled, or None."""
    body = text.strip("\n")
    if not body.strip():
        return "it is empty"
    if CONFLICT_MARKER.search(text):
        return "it holds a merge conflict marker"
    if public:
        lines = [line for line in body.split("\n") if line.strip()]
        if len(lines) != 1:
            return "a public fragment is one bullet on one line, and this has %d lines" % len(lines)
        if not lines[0].startswith("- **"):
            return "a public bullet starts with '- **'"
        return None
    if not body.startswith("### "):
        return "an internal entry starts with a '### ' heading"
    if any(re.match(r"#{1,2} ", line) for line in outside_code(body)):
        return "an entry has no '#' or '##' heading of its own; those are the changelog's"
    return None


def outside_code(text):
    """The lines of `text` that are not inside a ``` fence: a shell comment in
    a code block is not a heading."""
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield line


def load(repo=REPO):
    """(fragments, problems), every file in docs/changes/ but its README."""
    directory = os.path.join(repo, CHANGES_DIR)
    fragments, problems = [], []
    if not os.path.isdir(directory):
        return fragments, problems
    for name in sorted(os.listdir(directory)):
        if name == "README.md":
            continue
        path = os.path.join(directory, name)
        found = FRAGMENT_NAME.match(name)
        if not found:
            problems.append("%s: not a fragment name - <issue>.md, <issue>-<suffix>.md, "
                            "or the same with .public.md" % name)
            continue
        text = read(path)
        public = bool(found.group("public"))
        problem = fragment_problem(name, text, public)
        if problem:
            problems.append("%s: %s" % (name, problem))
            continue
        fragments.append(Fragment(path, int(found.group("number")), found.group("suffix") or "", public,
                                  text.strip("\n")))
    return fragments, problems


def insert_release(text, heading, block):
    """`text` with `heading` and `block` in front of its first `## ` heading."""
    match = re.search(r"^## ", text, re.M)
    at = match.start() if match else len(text)
    return text[:at] + heading + "\n\n" + block.rstrip("\n") + "\n\n" + text[at:]


def unreleased_problems(repo=REPO):
    """An Unreleased section left in either changelog would sit above the
    release and be filed under nothing."""
    found = []
    for rel in (INTERNAL, PUBLIC):
        if re.search(r"^## .*Unreleased", read(os.path.join(repo, rel)), re.M):
            found.append("%s still has an Unreleased section: move its entries into docs/changes/" % rel)
    return found


def roll(version, title, date=None, repo=REPO, log=print):
    """Write the release into both changelogs and delete the fragments.
    Returns 0, or 1 having changed nothing."""
    problems = []
    if not VERSION.match(version):
        problems.append("%r is not a version like v1.17.0" % version)
    if not str(title or "").strip() or "\n" in title:
        problems.append("the release needs a title, on one line")
    if date is not None and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(date)):
        problems.append("%r is not a date like 2026-10-12" % date)
    fragments, found = load(repo)
    problems += found + unreleased_problems(repo)
    internal = sorted((f for f in fragments if not f.public), key=lambda f: f.order, reverse=True)
    public = sorted((f for f in fragments if f.public), key=lambda f: f.order, reverse=True)
    if not internal and not problems:
        problems.append("there is nothing to roll: docs/changes/ holds no internal entry")
    if problems:
        for problem in problems:
            log("roll_changelog: " + problem)
        log("roll_changelog: nothing was changed.")
        return 1

    date = date or datetime.date.today().isoformat()
    internal_path, public_path = os.path.join(repo, INTERNAL), os.path.join(repo, PUBLIC)
    write(internal_path, insert_release(read(internal_path), '## \U0001F7E9 %s (%s) - "%s"' % (version, date, title),
                                        "\n\n".join(f.text for f in internal)))
    if public:
        write(public_path, insert_release(read(public_path), "## %s — %s" % (version, title),
                                          "\n".join(f.text for f in public)))
    for fragment in fragments:
        os.remove(fragment.path)
    log("roll_changelog: %s - %d internal entr%s and %d public bullet%s rolled; the fragments are deleted."
        % (version, len(internal), "y" if len(internal) == 1 else "ies", len(public), "" if len(public) == 1 else "s"))
    return 0


def check(repo=REPO, log=print):
    fragments, problems = load(repo)
    problems += unreleased_problems(repo)
    for problem in problems:
        log("roll_changelog: " + problem)
    if not problems:
        log("roll_changelog: %d fragment(s), all well formed." % len(fragments))
    return 1 if problems else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Roll docs/changes/ into a release in both changelogs.")
    parser.add_argument("version", nargs="?", help="the release, like v1.17.0")
    parser.add_argument("title", nargs="?", help="its title, in quotes")
    parser.add_argument("--date", help="YYYY-MM-DD; today if left out")
    parser.add_argument("--check", action="store_true", help="validate the fragments and change nothing")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    if not args.version or not args.title:
        parser.error("give the version and the title, or --check")
    return roll(args.version, args.title, args.date)


if __name__ == "__main__":
    sys.exit(main())
