"""BOT_WINDOW for start-dccore.bat (#1065), as an exit code.

A batch file cannot read settings.conf the way the daemon does, and should
not try: this reads it through defaults, the same as the daemon, and answers
with an exit code, which a batch file compares exactly - no output to parse,
no quoting of a python path inside FOR /F.

    0   normal (and anything this could not read)
    20  minimised
    21  hidden
"""

import contextlib
import io
import os
import sys

NORMAL, MINIMISED, HIDDEN = 0, 20, 21
CODES = {"minimised": MINIMISED, "hidden": HIDDEN}


def mode_code():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(root, "src"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            import defaults
        return CODES.get(str(getattr(defaults, "BOT_WINDOW", "normal")).strip().lower(), NORMAL)
    except Exception:
        return NORMAL


if __name__ == "__main__":
    sys.exit(mode_code())
