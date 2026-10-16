"""The dashboard's on-connect commands (#1320): the lines the bot sends when
it connects, saving them, and sending them again.

Split out of webserver.py, step 5 of splitting it (docs/FUTURE.md); the
routes that call this are still there, and webserver imports every name
back.
"""

import sys

import defaults as config


def build_on_connect_payload():
    """GET /api/on-connect: the commands sent once registered, and the gap.

    THE COMMANDS COME BACK IN FULL, and that is a decision rather than an
    oversight. One of them is very likely an X login with a password in it, so
    the alternative is a box an operator can overwrite but never read - which
    means never correcting a typo without retyping the lot.

    They are already behind the dashboard login, and anybody who has that can
    read data/on_connect.json off the disk anyway. What must NOT happen is the
    text reaching a log or a debug channel, and that is enforced where it would
    happen - see on_connect.redacted().
    """
    import on_connect

    commands, delay = on_connect.load()
    return {
        "commands": commands,
        "delay_seconds": delay,
        "max_commands": on_connect.MAX_COMMANDS,
        "max_delay_seconds": on_connect.MAX_DELAY_SECONDS,
    }


def apply_on_connect_changes(payload):
    """POST /api/on-connect: validate the whole set, then write it.

    Returns (http_status, payload_dict).

    NO REHASH. These are read fresh at every connect, so the next one picks
    them up - and rehashing to apply them would be misleading, because what
    they need is a reconnect, not a reload. The response says so.
    """
    import on_connect

    if not isinstance(payload, dict):
        return 400, {"error": "Expected an object with 'commands'."}

    raw = payload.get("commands")
    if isinstance(raw, str):
        # The page sends a textarea, one command per line, because that is what
        # an operator pastes. Splitting here rather than in the browser keeps
        # the API usable by hand.
        raw = raw.splitlines()
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return 400, {"error": "'commands' must be a list or a block of text."}

    delay = payload.get("delay_seconds", on_connect.DEFAULT_DELAY_SECONDS)

    try:
        written = on_connect.save(raw, delay)
    except ValueError as err:
        return 400, {"error": str(err)}
    except OSError as err:
        return 500, {"error": f"Could not write the commands: {err}"}

    return 200, {
        "commands": written,
        "delay_seconds": float(delay),
        "reconnect_required": bool(written),
        "message": ("Saved. They run when the bot next reconnects - or now, "
                    "with Resend commands." if written
                    else "Cleared. Nothing is sent on connect."),
    }


def build_on_connect_resend_result():
    """POST /api/on-connect/resend: send the SAVED commands to the server now
    (#1066). For when the automatic check gave up, or the operator wants the
    X login and +x again after a net split without reconnecting.

    Returns (http_status, payload_dict). The text of the commands is never in
    the answer or the log: an X login holds a password.
    """
    import on_connect

    commands, _gap = on_connect.load()
    if not commands:
        return 400, {"error": "No on-connect commands are saved."}
    oserve = sys.modules.get("oserve")
    sock = getattr(oserve, "irc_connection", None) if oserve else None
    if sock is None:
        return 409, {"error": "The bot is not connected, so nothing was sent."}
    # Not whether the channel is joined (#1085): a +r channel refuses the bot
    # exactly when the X login did not take, and sending the login again is
    # what this button is for. Only that this connection has sent its own,
    # so a press while it is still registering does not send them twice.
    if not on_connect.sent_on(getattr(config, "connection_epoch", None)):
        return 409, {"error": "The bot is still connecting; its on-connect "
                              "commands are about to go out, so nothing was sent."}
    try:
        sent = on_connect.resend_now(sock, getattr(config, "NICKNAME", ""))
    except Exception as err:
        return 500, {"error": f"Could not send them: {err}"}
    return 200, {"sent": sent,
                 "message": f"Sending {sent} on-connect command(s) to the server."}
