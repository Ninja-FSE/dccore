"""The dashboard's notices and private messages (#1320): the notices the bot
raises for the operator, and the inbox of messages strangers sent it,
with marking either as read.

Split out of webserver.py, step 5 of splitting it (docs/FUTURE.md); the
routes that call this are still there, and webserver imports every name
back.
"""

import defaults as config
import runtime


def build_notices_payload():
    """GET /api/notices: what the operator has not been told, and the rest.

    Newest FIRST here, and oldest first in storage. The store appends, which
    is the cheap end to write; a panel is read from the top, which is where
    the thing that just happened belongs. Reversing at the boundary keeps both
    ends natural rather than making one of them pay for the other.

    `unread` and `severity` are what the badge draws, and they are computed
    here rather than in the page: the page would have to know the read marker
    to work them out, and two places computing "is there anything new" is two
    places that can disagree about whether to light up.
    """
    import announce

    count, worst = announce.unread_notices()
    with runtime.notices_lock:
        rows = list(reversed(config.notices))
        seen = int(config.notice_state.get("seen_id", 0) or 0)

    return {
        "notices": rows,
        "unread": count,
        "severity": worst,
        "seen_id": seen,
    }


def mark_notices_read_result():
    """POST /api/notices/read: the operator has looked. Returns the payload
    the page would have got from a fresh GET, so the badge clears from the
    same answer that cleared it rather than from a second round trip that
    could race a notice arriving in between."""
    import announce

    announce.mark_notices_read()
    return build_notices_payload()


def messages_are_off():
    """True when this bot keeps no private messages at all."""
    return not getattr(config, "PRIVATE_MESSAGES_ENABLED", True)


def messages_payload_or_404():
    """(payload, status) for GET /api/messages.

    404, NOT an empty list. An empty list means "nobody has messaged you",
    which is a fact about the world; this is "there is no such page here",
    which is a fact about the bot. web/app.js hides the Messages nav item on
    exactly this status - the same signal the Console has used since it got
    an off-switch.

    Out here rather than inside the route because Flask is an optional
    dependency: a bot running without it still serves nothing, and logic that
    lives in a route body is logic no test in this repo can reach.
    """
    if messages_are_off():
        return {"error": "Private messages are turned off."}, 404
    return build_messages_payload(), 200


def messages_read_or_404():
    """(payload, status) for POST /api/messages/read. Same gate: turned off
    while somebody had the page open must not be a button that silently does
    nothing."""
    if messages_are_off():
        return {"error": "Private messages are turned off."}, 404
    return mark_messages_read_result(), 200


def build_messages_payload():
    """GET /api/messages: private messages nobody answered.

    Newest FIRST here and oldest first in storage, for the reason
    build_notices_payload() gives: appending is the cheap end to write, and a
    list is read from the top.

    No reply field and no per-message action that sends anything. The bot has
    no conversation path at all - see announce.record_private_message() - and
    an API that looked like it could answer would be a promise the daemon
    cannot keep.
    """
    import announce

    with runtime.private_messages_lock:
        rows = list(reversed(config.private_messages))
        seen = int(config.private_message_state.get("seen_id", 0) or 0)

    return {
        "messages": rows,
        "unread": announce.unread_private_messages(),
        "seen_id": seen,
    }


def mark_messages_read_result():
    """POST /api/messages/read: the operator has looked. Returns what a fresh
    GET would say, so the count and the list cannot disagree."""
    import announce

    announce.mark_private_messages_read()
    return build_messages_payload()
