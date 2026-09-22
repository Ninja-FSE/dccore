"""Print the ports the daemon listens on, for the scripts beside this file.

    python scripts/ports.py
    55000 55010 8420 0

The DCC range's first and last port, the dashboard's port, and 1/0 for
whether the dashboard is on - one line, space-separated, read from
settings.conf like the daemon itself reads them. A batch or shell script
cannot read settings.conf, and hard-coding the defaults would be wrong the
day an operator changes them - this is what the firewall and autostart
helpers run (#547, Proposal 6). The knowledge of which settings those are
lives in setup_check.ports_line(), the one place that knows the ports;
this file only prints it.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def main():
    for path in (REPO, HERE):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.chdir(REPO)
    import defaults as config
    import setup_check
    print(setup_check.ports_line(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
