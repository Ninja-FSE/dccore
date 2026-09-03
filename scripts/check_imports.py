"""Import every module in the project and report anything that will not load.

WHY THIS EXISTS AS A FILE RATHER THAN A ONE-LINER

The check used to be a hand-written `python -c "import a, b, c, ..."` in two
places: .github/workflows/tests.yml and scripts/preflight.py. Two hand-kept
lists of the same thing drift, and these did - in both directions at once:

    CI listed 11 modules.
    preflight listed 12 - it had platform_compat, CI did not.
    The project had 14.

adminchat.py and oserve.py were in neither. So a check named "every module
imports cleanly" had never once looked at the admin console - eleven hundred
lines including the whole authentication path - or at the daemon's own entry
point. A syntax error in either would have passed CI.

That is the same defect this project has now fixed four times in different
clothes: one fact written down in two places, agreeing only by luck. Issue #34
had the reader and writer of speed_record.txt disagreeing; the list side-files
were named twice; !list was compared case-sensitively in one place and
case-insensitively in another. The fix is always the same - derive it, do not
repeat it.

So the list is the filesystem. A new module at the repository root is covered
the moment it is created, without anybody remembering to add it here.
"""

import importlib
import os
import sys
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# admin_config.py is gitignored, machine-specific and optional. config.py
# already imports it when present and shrugs when it is not, so importing it
# here would either duplicate that or fail on every clean checkout.
SKIP = {"admin_config"}


def module_names():
    """Every importable module at the repository root, in a stable order."""
    names = []
    for entry in sorted(os.listdir(REPO)):
        if not entry.endswith(".py") or entry.startswith("_"):
            continue
        name = entry[:-3]
        if name in SKIP:
            continue
        names.append(name)
    return names


def main():
    sys.path.insert(0, REPO)
    os.chdir(REPO)

    names = module_names()
    if not names:
        print("check_imports: found no modules at the repository root - that is wrong")
        return 1

    failures = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:
            failures.append(name)
            print(f"  FAIL  {name}")
            traceback.print_exc()
        else:
            print(f"  ok    {name}")

    print()
    if failures:
        print(f"check_imports: {len(failures)} of {len(names)} module(s) failed to "
              f"import: {', '.join(failures)}")
        return 1

    print(f"check_imports: all {len(names)} modules import cleanly "
          f"({', '.join(names)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
