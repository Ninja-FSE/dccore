"""The dashboard's search broadcast (#1313): one @find sent into a channel,
and every bot's reply collected for half a minute.

Split out of webserver.py, step 1 of splitting it (docs/FUTURE.md); the
route that calls this is still there, and webserver imports these names
back. A test that changes BROADCAST_SEARCH_WINDOW changes it here: this
module's functions read their own.

Behind the same login as every other route - a logged-in operator can make
the daemon send an @find into a real, public IRC channel, which is why this
route, unlike most, actually sends something rather than just reading state.
"""

import sys
import threading
import time

import defaults as config
from web_common import reject_if_unsafe_for_irc_line


# Pure logic, no Flask import, fully unit testable - the same reasoning as
# webserver's build_*_payload() functions.
BROADCAST_SEARCH_WINDOW = 30.0        # seconds the listening window stays open


BROADCAST_SEARCH_MIN_TERM_LEN = 3     # mirrors list.py's own @find minimum


def build_broadcast_status_payload():
    """GET /api/search/broadcast/status payload: whether a window is open
    right now, plus every result captured so far. `listening` (not just
    the raw `broadcast_search_inprogress` flag) also checks the deadline, so
    a window that flipped false only a poll-interval ago and one that is
    genuinely still counting down are never confused."""
    now = time.time()
    deadline = float(getattr(config, "broadcast_search_deadline", 0) or 0)
    inprogress = bool(getattr(config, "broadcast_search_inprogress", False))
    return {
        "listening": inprogress and now < deadline,
        "deadline": deadline,
        "term": getattr(config, "broadcast_search_term", ""),
        "results": list(getattr(config, "broadcast_search_results", []) or []),
    }


def start_broadcast_search(term):
    """Validate and kick off a cross-bot @find broadcast. Returns
    (http_status, payload_dict); the Flask route below only parses the
    request body and calls this, so the actual behaviour - validation,
    cooldown, the in-memory state transition, queuing the outbound line - is
    exercised by tests/test_webserver.py with no Flask install required.

    Behind the same login as every other route in this app (see the module
    docstring) - a logged-in operator can make the daemon send an @find into
    a real, public IRC channel, which is why this route, unlike most others
    here, actually sends something rather than just reading state.
    """
    term = "" if term is None else term
    term_err = reject_if_unsafe_for_irc_line(term, "term")
    if term_err:
        return 400, {"error": term_err}

    clean_term = term.strip()
    if len(clean_term) < BROADCAST_SEARCH_MIN_TERM_LEN:
        return 400, {"error": f"Search term must be at least {BROADCAST_SEARCH_MIN_TERM_LEN} characters long."}

    now = time.time()
    if getattr(config, "broadcast_search_inprogress", False) and now < getattr(config, "broadcast_search_deadline", 0):
        return 409, {"error": "A broadcast search is already in progress.",
                     "deadline": config.broadcast_search_deadline}

    cooldown = float(getattr(config, "BROADCAST_SEARCH_COOLDOWN", 30))
    since_last = now - float(getattr(config, "last_broadcast_search_at", 0) or 0)
    if since_last < cooldown:
        return 429, {"error": f"Please wait {int(cooldown - since_last)}s before broadcasting another search."}

    oserve = sys.modules.get("oserve")
    if not oserve or not getattr(oserve, "irc_connection", None):
        return 503, {"error": "IRC connection is not up."}

    channel = (getattr(config, "BROADCAST_SEARCH_CHANNEL", None)
               or str(getattr(config, "CHANNEL", "")).split(",")[0].strip())
    if not channel:
        return 503, {"error": "No broadcast channel configured."}

    deadline = now + BROADCAST_SEARCH_WINDOW
    config.broadcast_search_inprogress = True
    config.broadcast_search_deadline = deadline
    config.broadcast_search_term = clean_term
    # In place, not `= []`: config.broadcast_search_results is bound from
    # runtime.py - see runtime.py's docstring on why this must never rebind.
    config.broadcast_search_results.clear()
    config.last_broadcast_search_at = now

    # reject_if_unsafe_for_irc_line() above already caps clean_term's length
    # (IRC_LINE_FIELD_MAX_LEN), so this is belt-and-braces: fit_irc_line()
    # shrinks against the real wire budget (announce.IRC_LINE_BUDGET) rather
    # than trusting that the boundary cap alone guarantees the built line
    # fits, the same defense-in-depth posture dcc_fetch.py's own dispatch
    # loop takes for the enqueue-time bot/filename check (#162 finding #13).
    import announce
    line = announce.fit_irc_line(lambda v: f"PRIVMSG {channel} :@find {v}\r\n", clean_term)
    oserve.queue_message(channel, line)

    def _close_window(expected_deadline=deadline):
        # Runs on its own daemon thread so the request thread returns
        # immediately - see the module docstring's "must not block the Flask
        # request thread for 30s" requirement.
        time.sleep(BROADCAST_SEARCH_WINDOW)
        # Only close OUR window. A fresh broadcast should never start before
        # this one's deadline given the cooldown above, but this guard means
        # a stale timer can never clobber a newer window if that invariant
        # is ever violated (a rehash resetting last_broadcast_search_at, say).
        if getattr(config, "broadcast_search_deadline", None) == expected_deadline:
            config.broadcast_search_inprogress = False

    threading.Thread(target=_close_window, daemon=True).start()

    return 200, {"status": "listening", "deadline": deadline, "term": clean_term}
