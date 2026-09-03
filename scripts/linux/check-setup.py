#!/usr/bin/env python3
"""Verify a Linux setup WITHOUT connecting to IRC.

Every check lives in scripts/setup_check.py, shared with the Windows launcher
so the two cannot drift apart. This file only says which platform it is.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import setup_check  # noqa: E402

if __name__ == "__main__":
    sys.exit(setup_check.main(setup_check.LINUX))
