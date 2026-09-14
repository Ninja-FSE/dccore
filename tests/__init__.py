"""Regression tests for DCCore.

Run from the repository root with:

    python -m unittest discover -s tests -t .

Stdlib only, so this works on the production LXC and on Windows with nothing
installed. Each test names the defect it guards against; the suite exists so a
future change cannot silently reintroduce one of them.

NOTHING IN THIS SUITE MAY OPEN A REAL BROWSER.

webserver.start() opens the dashboard in the operator's browser, and two tests
in test_webserver.py drive start() directly to check the WEBUI_ENABLED and
WEBUI_HOST gates. Those tests already replace app.run() so that a broken gate
cannot bind a real socket - "a test must not be able to start a live listener
because the code it is testing broke" - but the browser call sits one line
above that, and was missed. Every full-suite run launched
http://127.0.0.1:8420/ on the developer's desktop, minutes apart, on a machine
where the bot itself was not running.

The guard lives here rather than in tests/support.py because only 90 of the
121 test files import that harness, and the class that tripped this is a plain
unittest.TestCase. Importing this package is the one thing every run does.

Calls are RECORDED rather than dropped, so a test can still assert that the
dashboard would have been opened - see BROWSER_OPENS.
"""

import webbrowser as _webbrowser

# Every URL the suite tried to open, in order.
BROWSER_OPENS = []


def _record_browser_open(url, *_args, **_kwargs):
    BROWSER_OPENS.append(url)
    return True


_webbrowser.open = _record_browser_open
_webbrowser.open_new = _record_browser_open
_webbrowser.open_new_tab = _record_browser_open
