"""Regression tests for DCCore.

Run from the repository root with:

    python -m unittest discover -s tests -t .

Stdlib only, so this works on the production LXC and on Windows with nothing
installed. Each test names the defect it guards against; the suite exists so a
future change cannot silently reintroduce one of them.
"""
