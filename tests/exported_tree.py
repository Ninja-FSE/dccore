"""Telling this repository apart from an extracted public tree.

`docs/UPDATES.md` is export-ignored in `.gitattributes`. It is the internal
changelog: it exists in the development repository and, by design, never
reaches the public one that way - extraction step 3 renames
`docs/UPDATES-PUBLIC.md` onto that same path instead, so a released public
tree has a *different*, legitimate file sitting at `docs/UPDATES.md`.

That collision is why nothing here may use `docs/UPDATES.md`'s own presence
to decide which repository this is: checking it would read the post-rename
public changelog as "we must be in the development repository", which is
backwards. `docs/PUBLIC-REPO-WORKFLOW.md` has no such collision - nothing
ever renames a replacement onto its path in either repository, so its
presence or absence is the one signal this file trusts.

Getting the direction wrong is expensive either way:

  * Fail whenever the marker is missing, and the public repository's very
    first CI run is red - on the branch its own protection rules require to
    be green - for a file that was deliberately excluded.
  * Skip whenever the marker is missing, and the day it genuinely disappears
    from THIS repository, every test that guards internal-only content
    quietly stops running and nobody is told.

So the absence has to be EXPLAINED rather than assumed. `.gitattributes`
ships, so an extracted tree still carries the statement that the marker was
export-ignored (that line is left in place on purpose - see
docs/PUBLIC-REPO-WORKFLOW.md's own extraction checklist). If that statement
is there, the marker is missing for the one reason that is allowed. If it is
not, something else happened and the test should say so loudly.

Checking for `.git` instead would not work: the public repository is a git
repository too, so its presence says nothing about which tree this is.
"""

import io
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INTERNAL_CHANGELOG = os.path.join("docs", "UPDATES.md")

# The signal for "is this the development repository" - deliberately not
# INTERNAL_CHANGELOG itself. See this module's docstring.
WORKFLOW_DOC = os.path.join("docs", "PUBLIC-REPO-WORKFLOW.md")


def _is_export_ignored(relative_path):
    """True if .gitattributes says this path does not ship."""
    attributes = os.path.join(REPO_ROOT, ".gitattributes")
    if not os.path.exists(attributes):
        return False
    wanted = relative_path.replace(os.sep, "/")
    with io.open(attributes, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.split("#", 1)[0]
            fields = line.split()
            if len(fields) >= 2 and fields[0] == wanted:
                if "export-ignore" in fields[1:]:
                    return True
    return False


def _this_is_an_export(test_case):
    """True if this tree is a legitimately extracted public export rather
    than the development repository.

    Fails the test loudly rather than returning True when WORKFLOW_DOC is
    missing with no export-ignore rule to explain it - that combination is
    not an export, it is a fault, and the two must never read the same.
    """
    if os.path.exists(os.path.join(REPO_ROOT, WORKFLOW_DOC)):
        return False
    if not _is_export_ignored(WORKFLOW_DOC):
        test_case.fail(
            f"{WORKFLOW_DOC} is missing, and .gitattributes does not "
            f"export-ignore it - so it has gone missing for some other "
            f"reason than being left out of the public tree.")
    return True


def internal_file_or_skip(test_case, relative_path):
    """The absolute path of a file that exists here and deliberately never
    reaches the public repository, or skip if this is an export.

    Which tree this is gets decided by WORKFLOW_DOC, not by `relative_path`
    itself - see this module's docstring for why that distinction matters.
    Once this is confirmed to be the development repository, `relative_path`
    missing here is a plain fault: nothing explains that one away.
    """
    if _this_is_an_export(test_case):
        test_case.skipTest(
            f"{WORKFLOW_DOC} is export-ignored and absent, so this is an "
            f"extracted public tree rather than the development repository.")
    full = os.path.join(REPO_ROOT, relative_path)
    if not os.path.exists(full):
        test_case.fail(
            f"{relative_path} is missing from the development repository.")
    return full


def internal_changelog_or_skip(test_case):
    """The absolute path of docs/UPDATES.md, or skip if this is an export."""
    return internal_file_or_skip(test_case, INTERNAL_CHANGELOG)
