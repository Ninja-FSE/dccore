"""The dashboard's settings pages, over the admin console (#1264).

dccore.mrc's settings window does its work through the console, so an
operator can change the bot's settings from mIRC as they can on the
dashboard's Settings page. Every command here is a thin line protocol over
the dashboard's OWN functions in webserver.py - build_settings_payload() and
apply_settings_changes(), build_lists_payload() and apply_list_changes(),
build_folders_payload() and apply_folder_changes(), the on-connect trio and
the theme preview. Nothing is validated or written a second way: two
implementations of "save a setting" would disagree the first time one of
them changed, and the window would then save something the page refuses.

THE COMMANDS ARE THE CONSOLE'S ONLY. They sit in adminchat.COMMANDS, which
only an authenticated DCC CHAT session and the dashboard's own Console page
(behind the dashboard login) dispatch. The in-channel admin commands are a
fixed handful in irc.py and never reach this table - a test pins that.

TRANSACTIONS. `setbegin`, any number of `set`, then `setcommit` applies them
all in ONE apply_settings_changes() call, so the window's Apply costs one
rehash rather than one per field. The open transaction lives on the session
object, so a disconnect - or a second login taking the console over - drops
it with the session; there is nothing to clean up and no lock to hold, since
one session's commands run one at a time on its own reader thread. The
served lists, the served folders and the on-connect commands have the same
begin / rows / commit shape, because each of their dashboard endpoints takes
the WHOLE set and validates it as a set.

A dashboard Console request is a fresh session every time, so a transaction
opened there would be gone before its first row arrived - and the next `set`
would then apply at once while the operator believed it was buffered. The
transaction commands are refused there, with the reason.

THE VALUES CROSS AS ONE LINE, in their settings.conf form (see
settings_file.render(); a CUSTOM_THEME_* colour as its \\xHH escape text,
exactly as the Settings page shows it), through encode_value() below - which
changes nothing about an ordinary value and makes the rare awkward one (an
empty value, a leading, trailing or doubled space, a control character)
survive a client that collapses spaces, as mIRC does. docs/ADMIN-CONSOLE.md,
"Settings over the console", is the protocol's definition.

NOTHING HERE LOGS A VALUE. The console's dispatcher leaves the lines that
carry one out of the bot's log (see adminchat._UNLOGGED), and a commit logs
the names of what it saved. ADMIN_PASSWORD_HASH is never shown, never
accepted and never echoed; an on-connect command - which may well be an X
login with a password in it - is shown to the operator who asks, as the
dashboard shows it, and is never logged (on_connect.py's own rule).
"""

import math
import re
import time

import defaults as config
import runtime

# The protocol this module speaks, for `consolecaps`. A client reads it to
# tell a bot that has these commands from one that answers "Unknown command",
# and a later change that moves a field bumps the number of the part it moves.
CAPS = (("settings", 1), ("preview", 1), ("served", 1), ("folders", 1),
        ("onconnect", 1), ("banlist", 1), ("unlock", 1))

# Wrong `unlock` passwords a session gets before it is closed - the login's
# own allowance (adminchat.MAX_PASSWORD_ATTEMPTS), for the same guessing.
UNLOCK_ATTEMPTS = 3

# How many rows of each kind `banlist` sends at most. The session's outbox
# holds 500 lines and drops the OLDEST once full - BANBEGIN first - so a
# hard_bans.txt with thousands of patterns must not be poured into it whole.
# BANEND still carries the true totals, the split DQEND already makes.
BANLIST_MAX = 200


# --------------------------------------------------------------------------
# The value encoding
# --------------------------------------------------------------------------

# The ONLY escapes there are: %HH for a control character (00-1F, 7F), a
# space (20), a percent sign (25) and, in a token, a lone dash (2D). Any
# other `%` - "%admin%", "%nick%", "100%", "%41" - is just a percent sign,
# so the placeholders settings and on-connect commands are full of cross as
# they are typed.
_ESCAPE_RE = re.compile(r"%(0[0-9A-Fa-f]|1[0-9A-Fa-f]|20|25|2[Dd]|7[Ff])")


def encode_value(text):
    """A value as it goes on a line: the text itself, with exactly these
    characters written as %HH (two uppercase hex digits of the character):

      - a control character (0x00-0x1F, 0x7F);
      - a `%` that would otherwise read as one of these escapes;
      - a space at the start or the end of the value, and every space that
        follows another space.

    So "Some Album", "50% off" and "%admin%" cross unchanged, and nothing on
    the line depends on a run of spaces surviving the trip.
    """
    text = str("" if text is None else text)
    last = len(text) - 1
    out = []
    for index, ch in enumerate(text):
        code = ord(ch)
        if code < 32 or code == 127:
            out.append("%%%02X" % code)
        elif ch == "%" and _ESCAPE_RE.match(text, index):
            out.append("%25")
        elif ch == " " and (index == 0 or index == last or text[index - 1] == " "):
            out.append("%20")
        else:
            out.append(ch)
    return "".join(out)


def decode_value(text):
    """The way back: every escape is its character; nothing else changes.
    One pass, left to right, so `%2520` is `%20` and not a space."""
    return _ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 16)), str(text or ""))


def encode_token(text):
    """A value that is NOT the last field of its line, so it must stay one
    token: encode_value() with every space escaped, `-` for an empty value
    and `%2D` for a value that is exactly "-"."""
    text = str("" if text is None else text)
    if not text:
        return "-"
    if text == "-":
        return "%2D"
    return encode_value(text).replace(" ", "%20")


def decode_token(token):
    token = str(token or "")
    return "" if token == "-" else decode_value(token)


# --------------------------------------------------------------------------
# Replies
# --------------------------------------------------------------------------

def _fields(text, maxsplit):
    """`text` split on runs of ASCII spaces, at most `maxsplit` times; the
    last part keeps everything after it. ASCII spaces ONLY (#1264 review):
    str.split() and str.strip() also take a no-break space, U+3000 and the
    like, which the encoding does not escape - so a folder label
    "My<no-break space>Music" sent back unchanged came back as "My"."""
    text = str(text or "").strip(" ")
    return re.split(" +", text, maxsplit=maxsplit) if text else []


def _finite(text):
    """`text` as a finite float, or None: float() also reads nan and inf."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _send_lines(session, lines):
    """Send a snapshot whole: Session.send_lines() waits for room in the
    outbox rather than letting it drop the snapshot's first lines; a session
    without one (the dashboard Console's) just collects them."""
    bulk = getattr(session, "send_lines", None)
    if bulk is not None:
        bulk(lines)
        return
    for line in lines:
        session.send(line)


def _structured(session):
    return bool(getattr(session, "structured", False))


def _say(session, machine, prose):
    """One reply: the DCCORE line to a script, the sentence to a person.
    Either may be None to send nothing in that mode. A message is one line
    however it was written: a line break would end the DCCORE line early and
    send the rest as a line of its own."""
    if _structured(session):
        if machine is not None:
            session.send("DCCORE " + str(machine).replace("\r", " ").replace("\n", " "))
    elif prose is not None:
        session.send(prose)


def _holds_transactions(session):
    """A DCC CHAT session keeps state between its lines; the dashboard's
    Console makes a new session per request and keeps none."""
    return bool(getattr(session, "holds_transactions", False))


def _no_transactions_here(session, what):
    session.send(f"{what} needs a DCC CHAT console: this one forgets everything "
                 f"between commands, so a transaction opened here would be gone "
                 f"before its first line. Use the dashboard's own Settings page.")


def _log(session, text):
    print(f"[ADMINCHAT] {getattr(session, 'nick', '?')} {text}")


# --------------------------------------------------------------------------
# The lock: a token login reads, the password changes (#1264 audit)
# --------------------------------------------------------------------------

def is_locked(session):
    """Whether this session may not change settings yet: it logged in with a
    paired token and has not given the password. A token sits in clear text
    in the script's dccore.ini, and the dashboard asks for the password for
    everything these commands change - served folders (which can publish
    any directory on the machine), the on-connect commands (which hold an X
    login and go to the server as the bot), ADMIN_HOSTMASKS, the token store.
    A stand-in without the attribute (the dashboard's Console, behind its
    password login) is not locked."""
    return not getattr(session, "unlocked", True)


def refused_while_locked(session, what):
    """Refuse `what` on a locked session, saying how to unlock. True if
    refused. A transaction the session has open is left open, so the window
    can unlock and send its commit again."""
    if not is_locked(session):
        return False
    sentence = "Type unlock <password> first - a paired token alone cannot change settings."
    _say(session, f"LOCKED {what} {sentence}", sentence)
    return True


def unlock_password_matches(supplied):
    """Whether `supplied`, the argument of `unlock`, is the admin password:
    as it is, then decoded (#1281).

    The settings window sends a password with a run of spaces, or one at
    either end, encoded as a value is (encode_value()), since mIRC closes
    those up in a command's parameters - as the login's `DCCORE PASSWORD`
    form does (#1273). Every other password goes as typed, which is what a
    bot from before this checks, so the argument is tried as it is first:
    a password that merely looks encoded still unlocks. One call is one
    attempt, however many forms it tries."""
    import adminchat
    stored = getattr(config, "ADMIN_PASSWORD_HASH", "")
    if adminchat.verify_password(stored, supplied):
        return True
    decoded = decode_value(supplied)
    return bool(decoded) and decoded != supplied and adminchat.verify_password(stored, decoded)


def cmd_unlock(session, args):
    """`unlock <password>`: the admin password, checked the way the login
    checks it, unlocks a token session for the rest of its life. Wrong three
    times, the session is closed, as at the login. The password may come
    encoded (unlock_password_matches())."""
    import adminchat
    if not is_locked(session):
        _say(session, "UNLOCKED", "This session can change settings.")
        return
    supplied = str(args or "")
    # Counted against the address before it is verified, as at the login
    # (adminchat.reserve_password_attempt()): unlocks sent side by side from
    # one address share the attempt limit instead of each getting their own.
    # One reservation covers both forms unlock_password_matches() tries.
    if not adminchat.reserve_password_attempt(getattr(session, "peer_ip", "")):
        _log(session, f"was refused an unlock: the address is blocked after "
                      f"{adminchat.MAX_PASSWORD_ATTEMPTS} failed attempts.")
        session.close(announce_text="Too many failed password attempts from this address. Try again later.")
        adminchat._forget(session)
        return
    if unlock_password_matches(supplied):
        session.unlocked = True
        session.unlock_failures = 0
        adminchat.clear_bad_ip(getattr(session, "peer_ip", ""))
        _log(session, "unlocked a token session with the password.")
        _say(session, "UNLOCKED", "Unlocked: this session can change settings now.")
        return
    session.unlock_failures = getattr(session, "unlock_failures", 0) + 1
    # Already counted against the address by reserve_password_attempt().
    _log(session, f"gave a wrong unlock password, attempt {session.unlock_failures}/{UNLOCK_ATTEMPTS}.")
    if session.unlock_failures >= UNLOCK_ATTEMPTS:
        session.close(announce_text="Incorrect Password.")
        adminchat._forget(session)
        return
    time.sleep(adminchat.WRONG_PASSWORD_DELAY)
    if not str(getattr(config, "ADMIN_PASSWORD_HASH", "") or ""):
        message = "No admin password is set on this bot; set one on the dashboard first."
    else:
        message = f"Wrong password ({session.unlock_failures} of {UNLOCK_ATTEMPTS})."
    _say(session, f"UNLOCK error {message}", message)


# --------------------------------------------------------------------------
# consolecaps
# --------------------------------------------------------------------------

def machine_name():
    """This computer's name, as one token - what mIRC's $host gives on the
    same machine.

    The settings window's "..." buttons pick a path on the computer mIRC
    runs on, which is only the bot's if both are the same machine. The
    console connection's address cannot say so: a DCC chat to a bot on the
    same PC arrives from the public address the client advertises, not
    127.0.0.1 - the buttons were greyed out on exactly that setup, the
    common one. Spaces and colons, which would break the CAPS line's
    tokens, become "-".
    """
    import socket
    try:
        name = socket.gethostname()
    except OSError:
        name = ""
    name = "".join("-" if ch.isspace() or ch == ":" or ord(ch) < 32 else ch for ch in str(name))
    return name or "-"


def caps_line():
    return ("CAPS " + " ".join(f"{name}:{version}" for name, version in CAPS)
            + " machine:" + machine_name())


def cmd_consolecaps(session, args):
    _say(session, caps_line(),
         "This bot's console speaks: " + ", ".join(f"{name} (v{version})" for name, version in CAPS) + ".")


# --------------------------------------------------------------------------
# settings / set / setbegin / setcommit / setabort / setpreview
# --------------------------------------------------------------------------

def _settings_fields():
    """[(category_label, field)] of every field the Settings page gets, read
    the way the page reads it - build_settings_payload() takes
    runtime.config_reload_lock itself, so a dump never sees a reload half
    done."""
    import webserver
    payload = webserver.build_settings_payload()
    return [(category.get("label", ""), field)
            for category in payload.get("categories", [])
            for field in category.get("fields", [])
            if field.get("name") != "ADMIN_PASSWORD_HASH"]


def field_text(field):
    """A field's value in its settings.conf form, unencoded."""
    import settings_file
    return settings_file.render(field.get("value"))


def settings_lines(fields):
    """The `settings` snapshot:

        DCCORE SETBEGIN <count>
        DCCORE SETF <KEY> <type> <value>
        DCCORE SETEND <count>
    """
    lines = [f"DCCORE SETBEGIN {len(fields)}"]
    for _label, field in fields:
        value = encode_value(field_text(field))
        lines.append(f"DCCORE SETF {field['name']} {field.get('type', 'str')}"
                     + (f" {value}" if value else ""))
    lines.append(f"DCCORE SETEND {len(fields)}")
    return lines


def cmd_settings(session, args):
    """`settings [word]`: every setting the dashboard's Settings page offers,
    with its value - or only those whose name contains `word`."""
    word = str(args or "").strip(" ").upper()
    fields = [(label, field) for label, field in _settings_fields()
              if not word or word in field["name"]]
    if _structured(session):
        _send_lines(session, settings_lines(fields))
        return
    if not fields:
        session.send(f"No setting's name contains {word}." if word else "No settings.")
        return
    out = []
    shown = None
    for label, field in fields:
        if label != shown:
            out.append(f"[{label}]")
            shown = label
        out.append(f"  {field['name']} = {encode_value(field_text(field))}")
    out.append(f"{len(fields)} setting(s). Change one with: set <KEY> <value>")
    _send_lines(session, out)


class SettingsTransaction:
    """What `setbegin` opened: the settings the page offers when it was
    opened, the values buffered so far, and the keys refused along the way."""

    def __init__(self, fields):
        self.fields = {field["name"]: field for _label, field in fields}
        self.changes = {}
        self.refused = []
        # Opened by a lone `set` that is waiting for `setcommit confirm`;
        # see end_implicit_transaction().
        self.implicit = False


def file_values():
    """{NAME: raw text} of what settings.conf sets now, or {} when it cannot
    be read - read with settings_file's own parser."""
    import io as _io
    import settings_file
    try:
        with _io.open(settings_file.settings_path(), "rb") as handle:
            return settings_file.parse(handle.read().decode("utf-8-sig").replace("\r\n", "\n"))
    except (OSError, UnicodeDecodeError, settings_file.SettingsError):
        return {}


def _same_as_now(name, text, in_file=None):
    """Whether `text` reads back as the value `name` will have - the
    unchanged value the window sends back for a field nobody touched. Read
    with settings_file.coerce(), the reader settings.conf goes through.

    Judged against what settings.conf holds for `name` when it sets it, and
    against config only when it does not (#1264 audit). A save rehashes on
    a thread that first waits up to REHASH_TRANSFER_WAIT for transfers, and
    until it reloads, config still holds the OLD value: putting a setting
    back in that window was judged "Nothing changed", and the file kept the
    new value, which the rehash then applied. `in_file` is file_values(),
    read once by a caller that judges many."""
    import settings_file
    current = getattr(config, name, None)
    declared = settings_file.declared_types(vars(config)).get(name)
    try:
        value = settings_file.coerce(name, text, current, declared)
    except ValueError:
        return False
    stored = (file_values() if in_file is None else in_file).get(name)
    if stored is not None:
        try:
            current = settings_file.coerce(name, stored, current, declared)
        except ValueError:
            pass        # unreadable in the file: the daemon kept config's value
    # bool is an int: True == 1, so a flag and a number never count as one.
    if isinstance(value, bool) != isinstance(current, bool):
        return False
    return value == current


def _refusal(txn, name, text):
    """Why `name` cannot be set to `text`, in the dashboard's words, or None."""
    import settings_file
    if name == "ADMIN_PASSWORD_HASH":
        return ("the admin password is not changed with set: use the password "
                "form on the dashboard's Settings page, or run python src/adminchat.py.")
    if name not in txn.fields:
        return "not a setting the dashboard's Settings page offers."
    # Under the reload lock, as build_settings_payload() reads (#1264
    # review): inside a rehash's reload window config briefly holds
    # defaults.py's literals, and a value judged against that is judged
    # against a setting nobody has.
    with runtime.config_reload_lock:
        if _same_as_now(name, text):
            return None
        try:
            settings_file.check_change(vars(config), name, text)
        except settings_file.SettingsWriteError as err:
            return str(err)
    return None


def _buffer(session, txn, name, text):
    """Check one value and hold it in `txn`. True if it was taken."""
    problem = _refusal(txn, name, text)
    if problem:
        txn.refused.append(name)
        _say(session, f"SETERR {name} {problem}", f"{name}: refused - {problem}")
        return False
    txn.changes[name] = text
    return True


def _parse_set(args):
    """(KEY, decoded value) from `set`'s arguments, or (None, None)."""
    parts = _fields(args, 1)
    if not parts:
        return None, None
    return parts[0].upper(), decode_value(parts[1] if len(parts) > 1 else "")


def cmd_setbegin(session, args):
    if not _holds_transactions(session):
        _no_transactions_here(session, "setbegin")
        return
    previous = getattr(session, "settings_txn", None)
    dropped = len(previous.changes) if previous is not None else 0
    session.settings_txn = SettingsTransaction(_settings_fields())
    _say(session, f"SETOPEN {dropped}",
         ("Started a new transaction; the one open before it was dropped "
          f"({dropped} change(s) not saved). " if previous is not None else "Transaction open. ")
         + "set <KEY> <value> as often as you like, then setcommit (or setabort).")


def cmd_set(session, args):
    """`set <KEY> <value>`: inside a transaction, buffer it; outside one,
    save it now - a transaction of one."""
    name, text = _parse_set(args)
    if name is None:
        session.send("Usage: set <KEY> <value>   (an empty value clears it; `settings` lists them)")
        return
    txn = getattr(session, "settings_txn", None)
    if txn is not None:
        if _buffer(session, txn, name, text) and not _structured(session):
            session.send(f"{name} will be saved at setcommit ({len(txn.changes)} buffered).")
        return
    if refused_while_locked(session, "set"):
        return
    txn = SettingsTransaction(_settings_fields())
    _buffer(session, txn, name, text)
    _commit(session, txn, confirmed=False, single=True)


def cmd_setabort(session, args):
    txn = getattr(session, "settings_txn", None)
    session.settings_txn = None
    if txn is None:
        _say(session, "SETDONE error No transaction is open.", "No transaction is open.")
        return
    _say(session, f"SETDONE aborted {len(txn.changes)}",
         f"Dropped {len(txn.changes)} buffered change(s); nothing was saved.")


def cmd_setcommit(session, args):
    if not _holds_transactions(session):
        _no_transactions_here(session, "setcommit")
        return
    txn = getattr(session, "settings_txn", None)
    if txn is None:
        _say(session, "SETDONE error No transaction is open - send setbegin first.",
              "No transaction is open - start one with setbegin.")
        return
    if refused_while_locked(session, "setcommit"):
        return
    confirmed = str(args or "").strip(" ").lower() == "confirm"
    _commit(session, txn, confirmed=confirmed, single=False)


def _commit(session, txn, confirmed, single):
    """Save `txn` in ONE apply_settings_changes() call, the Settings page's
    own save, and say how it went."""
    import webserver

    if txn.refused:
        session.settings_txn = None
        names = ", ".join(txn.refused)
        _say(session, f"SETDONE error {len(txn.refused)} setting(s) refused ({names}); nothing was saved.",
             f"Nothing was saved: {names} refused.")
        return

    changes = {}
    unchanged = 0
    # The reads under the reload lock (see _refusal()); the save itself is
    # not, since its rehash takes the lock on a thread of its own.
    in_file = file_values()
    with runtime.config_reload_lock:
        for name, text in txn.changes.items():
            if _same_as_now(name, text, in_file):
                unchanged += 1
            else:
                changes[name] = text
        old_debug = str(getattr(config, "DEBUG_CHANNEL", "") or "").strip()
    if not changes:
        session.settings_txn = None
        _say(session, f"SETDONE ok 0 {unchanged} - Nothing changed; nothing was saved.",
             "Nothing changed; nothing was saved.")
        return

    # The question the dashboard's Save asks in a confirm() popup before it
    # sends confirm_debug_channel_removed (#1008 follow-up) - asked here, and
    # the transaction kept, so the answer is one more line: `setcommit confirm`.
    clearing = ("DEBUG_CHANNEL" in changes and not changes["DEBUG_CHANNEL"].strip()
                and bool(old_debug))
    if clearing and not confirmed:
        question = (f"Remove the debug channel, {old_debug}? The bot leaves {old_debug} as "
                    f"soon as this save completes. Nothing else about the setting changes - "
                    f"you can set a debug channel again later.")
        if not _holds_transactions(session):
            session.settings_txn = None
            session.send(question + " Clear it on the dashboard's Settings page, which asks first.")
            return
        session.settings_txn = txn
        txn.implicit = txn.implicit or single
        _say(session, f"SETDONE confirm {question}",
             f"{question} Type setcommit confirm to save{' it' if single else ''}, or setabort.")
        return

    session.settings_txn = None
    body = dict(changes)
    if clearing:
        body["confirm_debug_channel_removed"] = True
    import adminchat
    import threading
    reported = threading.Event()

    def applied():
        # On the rehash's thread, once it has run: the window reloads its
        # page now, not at SETDONE, when config still held the old values.
        # After SETDONE, always - the rehash can finish first.
        reported.wait(10)
        _say(session, "SETAPPLIED", "The rehash has finished: the new settings are in effect.")

    try:
        status, result = webserver.apply_settings_changes(
            body, source=(session.nick, adminchat.CONSOLE_SOURCE), on_applied=applied)
        if status != 200:
            message = str((result or {}).get("error") or "The settings could not be saved.")
            _say(session, f"SETDONE error {message}", f"Nothing was saved: {message}")
            return
        _report_saved(session, changes, unchanged, result)
    finally:
        reported.set()


def _report_saved(session, changes, unchanged, result):
    """SETDONE ok, and the log line, for a save that went through."""
    _log(session, f"saved {len(changes)} setting(s) from the console: {', '.join(sorted(changes))}")
    restart = list(result.get("restart_required") or [])
    message = f"Saved {len(changes)} setting(s); the bot is rehashing to apply them."
    if restart:
        message += f" Restart the bot to apply {', '.join(restart)}."
    _say(session, f"SETDONE ok {len(changes)} {unchanged} {','.join(restart) or '-'} {message}", message)


def end_implicit_transaction(session, command, args):
    """A lone `set` that asked for confirmation left a transaction open for
    `setcommit confirm`. Anything else the console is sent closes it first -
    `SETDONE aborted <n>` - so a later lone `set` saves at once, as it says,
    instead of being buffered silently into a transaction nobody opened."""
    txn = getattr(session, "settings_txn", None)
    if txn is None or not getattr(txn, "implicit", False):
        return
    if command == "setabort" or (command == "setcommit"
                                 and str(args or "").strip(" ").lower() == "confirm"):
        return
    session.settings_txn = None
    _say(session, f"SETDONE aborted {len(txn.changes)}",
         f"Not saved: {', '.join(txn.changes) or 'nothing'} (it was waiting for setcommit confirm).")


def _preview_body(txn):
    """What the Settings page posts to /api/settings/theme-preview, from the
    buffered (unsaved) values: THEME and the colours as the text the page
    holds, SEARCH_ENABLED as a real boolean (#1249 review)."""
    import settings_file
    body = {}
    for name, text in (txn.changes.items() if txn is not None else ()):
        if name == "THEME" or name.startswith("CUSTOM_THEME_"):
            body[name] = text
        elif name == "SEARCH_ENABLED":
            try:
                body[name] = bool(settings_file.coerce(name, text, getattr(config, name, True), bool))
            except ValueError:
                pass
    return body


def preview_lines(preview):
    """The theme preview, for a client to echo in colour:

        DCCORE PVBEGIN 2
        DCCORE PVLINE advert <line>
        DCCORE PVLINE notice <line>
        DCCORE PVEND 2

    Each line is encoded like every other value (encode_value), colour codes
    included. It used to go raw, and mIRC hands a chat line's text to a
    script with every run of spaces collapsed to one - and a theme's frame
    IS runs of spaces, painted with a background colour. Encoded, the runs
    survive as %20 and the client puts them back.
    """
    rows = [("advert", preview.get("advert", "")), ("notice", preview.get("notice", ""))]
    lines = [f"DCCORE PVBEGIN {len(rows)}"]
    for kind, line in rows:
        lines.append(f"DCCORE PVLINE {kind} " + encode_value(str(line).replace("\r", " ").replace("\n", " ")))
    lines.append(f"DCCORE PVEND {len(rows)}")
    return lines


def cmd_setpreview(session, args):
    """`setpreview`: the sample advert and notice in the theme the open
    transaction would save - or the saved one, with none open."""
    import webserver
    body = _preview_body(getattr(session, "settings_txn", None))
    preview = webserver.build_theme_preview(webserver.theme_preview_overrides(body))
    if _structured(session):
        for line in preview_lines(preview):
            session.send(line)
        return
    session.send(f"Advert: {preview.get('advert', '')}")
    session.send(f"Notice: {preview.get('notice', '')}")


# --------------------------------------------------------------------------
# served: the lists this bot serves (build_lists_payload / apply_list_changes)
# --------------------------------------------------------------------------

def _mode_of(row, channel):
    import library
    return (row.get("modes") or {}).get(str(channel).lower(), library.NORMAL)


def served_lines(payload):
    """The `served` snapshot:

        DCCORE SRVBEGIN <lists> <source> <max_lists>
        DCCORE SRVLIST <n> <primary 0|1> <name>
        DCCORE SRVCHAN <n> <channel> <mode>
        DCCORE SRVFOLDER <n> <label> <path>
        DCCORE SRVEND <lists> <channels> <folders>
    """
    rows = payload.get("lists") or []
    lines = [f"DCCORE SRVBEGIN {len(rows)} {payload.get('source', 'file')} "
             f"{payload.get('max_lists', 0)}"]
    chans = folders = 0
    for index, row in enumerate(rows, start=1):
        lines.append(f"DCCORE SRVLIST {index} {1 if row.get('primary') else 0} "
                     f"{encode_value(row.get('name', ''))}".rstrip(" "))
        for channel in row.get("channels") or []:
            chans += 1
            lines.append(f"DCCORE SRVCHAN {index} {encode_token(channel)} {_mode_of(row, channel)}")
        for folder in row.get("folders") or []:
            folders += 1
            lines.append(f"DCCORE SRVFOLDER {index} {encode_token(folder.get('name', ''))} "
                         f"{encode_value(folder.get('path', ''))}".rstrip(" "))
    lines.append(f"DCCORE SRVEND {len(rows)} {chans} {folders}")
    return lines


class ServedTransaction:
    def __init__(self):
        self.lists = []
        self.problems = []


def _refused_set(session, prefix, error):
    """A whole-set save the dashboard's function refused. library and
    on_connect name every problem at once, one per line, so an operator
    fixing three things hears about three: one <prefix>ERR line each, then
    <prefix>DONE error - carrying the message itself when there was one."""
    problems = [line for line in str(error).split("\n") if line.strip()] or ["Nothing was saved."]
    if len(problems) == 1:
        _say(session, f"{prefix}DONE error {problems[0]}", f"Nothing was saved: {problems[0]}")
        return
    for problem in problems:
        _say(session, f"{prefix}ERR {problem}", f"  {problem}")
    _say(session, f"{prefix}DONE error {len(problems)} problems; nothing was saved.",
         f"Nothing was saved: the {len(problems)} problems above.")


def _served_problem(session, txn, message):
    txn.problems.append(message)
    _say(session, f"SRVERR {message}", f"Refused: {message}")


def _served_index(txn, word):
    try:
        index = int(word)
    except (TypeError, ValueError):
        return None
    return index if 1 <= index <= len(txn.lists) else None


def _served_row(session, txn, sub, rest):
    import library
    if sub == "list":
        parts = _fields(rest, 2)
        if len(parts) < 2:
            _served_problem(session, txn, "served list needs <n> <primary 0|1> <name>.")
            return
        if parts[0] != str(len(txn.lists) + 1):
            _served_problem(session, txn, f"served list {parts[0]}: the next list is number {len(txn.lists) + 1}.")
            return
        if parts[1] not in ("0", "1"):
            _served_problem(session, txn, f"served list {parts[0]}: primary is 0 or 1, not {parts[1]}.")
            return
        txn.lists.append({"name": decode_value(parts[2] if len(parts) > 2 else ""),
                          "primary": parts[1] == "1", "channels": [], "modes": {}, "folders": []})
        return
    if sub == "chan":
        parts = _fields(rest, 3)
        index = _served_index(txn, parts[0]) if parts else None
        if index is None or len(parts) != 3:
            _served_problem(session, txn, "served chan needs <n> <channel> <mode>, <n> a list already sent.")
            return
        channel, mode = decode_token(parts[1]), parts[2].lower()
        if mode not in library.MODES:
            _served_problem(session, txn, f"{channel}: the mode is one of {', '.join(library.MODES)}, not {parts[2]}.")
            return
        row = txn.lists[index - 1]
        row["channels"].append(channel)
        if mode != library.NORMAL:
            row["modes"][channel.lower()] = mode
        return
    if sub == "folder":
        parts = _fields(rest, 2)
        index = _served_index(txn, parts[0]) if parts else None
        if index is None or len(parts) < 2:
            _served_problem(session, txn, "served folder needs <n> <label> <path>, <n> a list already sent.")
            return
        txn.lists[index - 1]["folders"].append(
            {"name": decode_token(parts[1]), "path": decode_value(parts[2] if len(parts) > 2 else "")})


def _served_commit(session, txn):
    import webserver
    session.served_txn = None
    if txn.problems:
        _say(session, f"SRVDONE error {len(txn.problems)} line(s) refused; nothing was saved.",
             "Nothing was saved: some lines were refused.")
        return
    current = webserver.build_lists_payload()
    if txn.lists == current.get("lists"):
        _say(session, "SRVDONE unchanged Nothing changed; nothing was saved.",
             "Nothing changed; nothing was saved.")
        return
    status, result = webserver.apply_list_changes({"lists": txn.lists})
    if status != 200:
        _refused_set(session, "SRV", (result or {}).get("error") or "The lists could not be saved.")
        return
    _log(session, f"saved {len(txn.lists)} served list(s) from the console.")
    message = str(result.get("message") or "Saved.")
    _say(session, f"SRVDONE ok {len(txn.lists)} {message}", message)


def cmd_served(session, args):
    """`served`: the lists this bot serves; `served begin|list|chan|folder|
    commit|abort ...`: replace them, as the dashboard's lists page does."""
    import webserver
    sub, _, rest = str(args or "").strip(" ").partition(" ")
    sub = sub.lower()
    if not sub:
        payload = webserver.build_lists_payload()
        if _structured(session):
            _send_lines(session, served_lines(payload))
            return
        rows = payload.get("lists") or []
        implied = " (none configured: this is the one list over the served folders)" \
            if payload.get("source") == "implied" else ""
        out = [f"{len(rows)} list(s){implied}:"]
        for index, row in enumerate(rows, start=1):
            out.append(f"  {index}. {row.get('name')}{' (primary)' if row.get('primary') else ''}")
            for channel in row.get("channels") or []:
                out.append(f"       {channel}  {_mode_of(row, channel)}")
            for folder in row.get("folders") or []:
                out.append(f"       {folder.get('name')} = {folder.get('path')}")
        _send_lines(session, out)
        return
    if sub not in ("begin", "list", "chan", "folder", "commit", "abort"):
        session.send("Usage: served   |   served begin, served list <n> <0|1> <name>, "
                     "served chan <n> <channel> <normal|quiet|request_only>, "
                     "served folder <n> <label> <path>, served commit | served abort")
        return
    if not _holds_transactions(session):
        _no_transactions_here(session, f"served {sub}")
        return
    txn = getattr(session, "served_txn", None)
    if sub == "begin":
        session.served_txn = ServedTransaction()
        dropped = len(txn.lists) if txn is not None else 0
        _say(session, f"SRVOPEN {dropped}",
             "Lists transaction open: served list / chan / folder, then served commit.")
        return
    if txn is None:
        _say(session, "SRVDONE error No lists transaction is open - send served begin first.",
             "No lists transaction is open - start one with served begin.")
        return
    if sub == "abort":
        session.served_txn = None
        _say(session, f"SRVDONE aborted {len(txn.lists)}", "Dropped; nothing was saved.")
        return
    if sub == "commit":
        if refused_while_locked(session, "served commit"):
            return
        _served_commit(session, txn)
        return
    _served_row(session, txn, sub, rest)


# --------------------------------------------------------------------------
# folders: the served folders of a single-list bot (build_folders_payload /
# apply_folder_changes) - the dashboard's "Your list" folder rows.
# --------------------------------------------------------------------------

def folders_lines(payload):
    """The `folders` snapshot:

        DCCORE FLDBEGIN <count> <source>
        DCCORE FLDROW <n> <label> <path>
        DCCORE FLDEND <count>
    """
    rows = payload.get("folders") or []
    lines = [f"DCCORE FLDBEGIN {len(rows)} {payload.get('source', 'none')}"]
    for index, row in enumerate(rows, start=1):
        lines.append(f"DCCORE FLDROW {index} {encode_token(row.get('name', ''))} "
                     f"{encode_value(row.get('path', ''))}".rstrip(" "))
    lines.append(f"DCCORE FLDEND {len(rows)}")
    return lines


class FoldersTransaction:
    def __init__(self):
        self.folders = []
        self.problems = []


def cmd_folders(session, args):
    """`folders`: the served folders; `folders begin|row|commit|abort ...`:
    replace them, as the dashboard's folder rows do."""
    import webserver
    sub, _, rest = str(args or "").strip(" ").partition(" ")
    sub = sub.lower()
    if not sub:
        payload = webserver.build_folders_payload()
        if _structured(session):
            _send_lines(session, folders_lines(payload))
            return
        rows = payload.get("folders") or []
        out = [f"{len(rows)} served folder(s) (source: {payload.get('source')}):"]
        for index, row in enumerate(rows, start=1):
            out.append(f"  {index}. {row.get('name')} = {row.get('path')}")
        _send_lines(session, out)
        return
    if sub not in ("begin", "row", "commit", "abort"):
        session.send("Usage: folders   |   folders begin, folders row <n> <label> <path>, "
                     "folders commit | folders abort")
        return
    if not _holds_transactions(session):
        _no_transactions_here(session, f"folders {sub}")
        return
    txn = getattr(session, "folders_txn", None)
    if sub == "begin":
        session.folders_txn = FoldersTransaction()
        dropped = len(txn.folders) if txn is not None else 0
        _say(session, f"FLDOPEN {dropped}", "Folders transaction open: folders row, then folders commit.")
        return
    if txn is None:
        _say(session, "FLDDONE error No folders transaction is open - send folders begin first.",
             "No folders transaction is open - start one with folders begin.")
        return
    if sub == "abort":
        session.folders_txn = None
        _say(session, f"FLDDONE aborted {len(txn.folders)}", "Dropped; nothing was saved.")
        return
    if sub == "row":
        parts = _fields(rest, 2)
        if len(parts) < 2 or parts[0] != str(len(txn.folders) + 1):
            message = f"folders row needs <n> <label> <path>, <n> being {len(txn.folders) + 1}."
            txn.problems.append(message)
            _say(session, f"FLDERR {message}", f"Refused: {message}")
            return
        txn.folders.append({"name": decode_token(parts[1]),
                            "path": decode_value(parts[2] if len(parts) > 2 else "")})
        return
    # commit
    if refused_while_locked(session, "folders commit"):
        return
    session.folders_txn = None
    if txn.problems:
        _say(session, f"FLDDONE error {len(txn.problems)} line(s) refused; nothing was saved.",
             "Nothing was saved: some lines were refused.")
        return
    if txn.folders == (webserver.build_folders_payload().get("folders") or []):
        _say(session, "FLDDONE unchanged Nothing changed; nothing was saved.",
             "Nothing changed; nothing was saved.")
        return
    status, result = webserver.apply_folder_changes({"folders": txn.folders})
    if status != 200:
        for problem in (result or {}).get("problems") or []:
            _say(session, f"FLDERR {problem}", f"  {problem}")
        message = str((result or {}).get("error") or "The folders could not be saved.")
        _say(session, f"FLDDONE error {message}", f"Nothing was saved: {message}")
        return
    _log(session, f"saved {len(txn.folders)} served folder(s) from the console.")
    _say(session, f"FLDDONE ok {len(txn.folders)} Saved. Rebuild the list to publish them.",
         "Saved. Rebuild the list (update) to publish them.")


# --------------------------------------------------------------------------
# onconnect: the commands sent once registered (build_on_connect_payload /
# apply_on_connect_changes / build_on_connect_resend_result)
# --------------------------------------------------------------------------

def _seconds(value):
    return f"{float(value):g}"


def onconnect_lines(payload):
    """The `onconnect` snapshot:

        DCCORE OCBEGIN <count> <delay_seconds> <max_commands> <max_delay_seconds>
        DCCORE OCLINE <n> <command>
        DCCORE OCEND <count>
    """
    commands = payload.get("commands") or []
    lines = [f"DCCORE OCBEGIN {len(commands)} {_seconds(payload.get('delay_seconds', 0))} "
             f"{payload.get('max_commands', 0)} {_seconds(payload.get('max_delay_seconds', 0))}"]
    for index, command in enumerate(commands, start=1):
        lines.append(f"DCCORE OCLINE {index} {encode_value(command)}".rstrip(" "))
    lines.append(f"DCCORE OCEND {len(commands)}")
    return lines


class OnConnectTransaction:
    def __init__(self, delay):
        self.commands = []
        self.delay = delay
        self.problems = []


def _onconnect_problem(session, txn, message):
    txn.problems.append(message)
    _say(session, f"OCERR {message}", f"Refused: {message}")


def cmd_onconnect(session, args):
    """`onconnect`: the commands sent to the server once registered;
    `onconnect begin|delay|line|commit|abort ...`: replace them;
    `onconnect resend`: send the saved ones again now."""
    import webserver
    sub, _, rest = str(args or "").strip(" ").partition(" ")
    sub = sub.lower()
    if not sub:
        # Locked too: an X login among them holds a password.
        if refused_while_locked(session, "onconnect"):
            return
        payload = webserver.build_on_connect_payload()
        if _structured(session):
            _send_lines(session, onconnect_lines(payload))
            return
        commands = payload.get("commands") or []
        out = [f"{len(commands)} on-connect command(s), "
               f"{_seconds(payload.get('delay_seconds', 0))} s apart:"]
        for index, command in enumerate(commands, start=1):
            out.append(f"  {index}. {command}")
        _send_lines(session, out)
        return
    if sub == "resend":
        if refused_while_locked(session, "onconnect resend"):
            return
        status, result = webserver.build_on_connect_resend_result()
        if status == 200:
            _say(session, f"OCRESEND ok {result.get('sent', 0)} {result.get('message', '')}".rstrip(),
                 str(result.get("message", "")))
        else:
            message = str((result or {}).get("error") or "Nothing was sent.")
            _say(session, f"OCRESEND error {message}", message)
        return
    if sub not in ("begin", "delay", "line", "commit", "abort"):
        session.send("Usage: onconnect   |   onconnect begin, onconnect delay <seconds>, "
                     "onconnect line <n> <command>, onconnect commit | onconnect abort   |   onconnect resend")
        return
    if not _holds_transactions(session):
        _no_transactions_here(session, f"onconnect {sub}")
        return
    txn = getattr(session, "onconnect_txn", None)
    if sub == "begin":
        session.onconnect_txn = OnConnectTransaction(
            float(webserver.build_on_connect_payload().get("delay_seconds", 0)))
        dropped = len(txn.commands) if txn is not None else 0
        _say(session, f"OCOPEN {dropped}",
             "On-connect transaction open: onconnect line <n> <command> (and onconnect delay), "
             "then onconnect commit.")
        return
    if txn is None:
        _say(session, "OCDONE error No on-connect transaction is open - send onconnect begin first.",
             "No on-connect transaction is open - start one with onconnect begin.")
        return
    if sub == "abort":
        session.onconnect_txn = None
        _say(session, f"OCDONE aborted {len(txn.commands)}", "Dropped; nothing was saved.")
        return
    if sub == "delay":
        delay = _finite(rest.strip(" "))
        if delay is None:
            _onconnect_problem(session, txn, f"the delay is a number of seconds, not {rest.strip(' ') or 'nothing'}.")
            return
        txn.delay = delay
        return
    if sub == "line":
        parts = _fields(rest, 1)
        number, command = (parts + ["", ""])[:2]
        if number != str(len(txn.commands) + 1):
            # Never the command itself in the answer: it may be a login.
            _onconnect_problem(session, txn, f"onconnect line needs <n> <command>, <n> being {len(txn.commands) + 1}.")
            return
        txn.commands.append(decode_value(command))
        return
    # commit
    if refused_while_locked(session, "onconnect commit"):
        return
    session.onconnect_txn = None
    if txn.problems:
        _say(session, f"OCDONE error {len(txn.problems)} line(s) refused; nothing was saved.",
             "Nothing was saved: some lines were refused.")
        return
    current = webserver.build_on_connect_payload()
    if (txn.commands == (current.get("commands") or [])
            and txn.delay == float(current.get("delay_seconds", 0))):
        _say(session, "OCDONE unchanged Nothing changed; nothing was saved.",
             "Nothing changed; nothing was saved.")
        return
    status, result = webserver.apply_on_connect_changes(
        {"commands": txn.commands, "delay_seconds": txn.delay})
    if status != 200:
        _refused_set(session, "OC", (result or {}).get("error") or "The commands could not be saved.")
        return
    _log(session, f"saved {len(result.get('commands') or [])} on-connect command(s) from the console.")
    message = str(result.get("message") or "Saved.")
    _say(session, f"OCDONE ok {len(result.get('commands') or [])} {message}", message)


# --------------------------------------------------------------------------
# banlist: the bans, framed for a window (`bans` stays the prose for a person)
# --------------------------------------------------------------------------

def banlist_lines(patterns, timed):
    """
        DCCORE BANBEGIN <permanent> <timed>
        DCCORE BANP <pattern>
        DCCORE BANT <seconds_left> <nick>
        DCCORE BANEND <permanent> <timed>

    `timed` is [(nick, seconds_left)]. At most BANLIST_MAX rows of each kind;
    the counts are always the true totals.
    """
    lines = [f"DCCORE BANBEGIN {len(patterns)} {len(timed)}"]
    for pattern in patterns[:BANLIST_MAX]:
        lines.append(f"DCCORE BANP {encode_value(pattern)}")
    for nick, left in timed[:BANLIST_MAX]:
        lines.append(f"DCCORE BANT {int(left)} {encode_token(nick)}")
    lines.append(f"DCCORE BANEND {len(patterns)} {len(timed)}")
    return lines


def current_bans():
    """(patterns, [(nick, seconds_left)]): the permanent wildcard patterns
    from hard_bans.txt and every timed ban or ignore still running - one
    table holds both, so the bot cannot tell an ignore from a flood ban."""
    import db
    import security
    patterns = list(db.load_hard_bans())
    timed = []
    for nick in sorted(dict(getattr(config, "banned_users", {}) or {})):
        left = security.ban_seconds_left(nick)
        if left > 0:
            timed.append((nick, left))
    return patterns, timed


def cmd_banlist(session, args):
    """`banlist`: the bans as a framed snapshot for a window; a person gets
    the same rows as `bans` shows them."""
    import security
    patterns, timed = current_bans()
    if _structured(session):
        _send_lines(session, banlist_lines(patterns, timed))
        return
    out = [f"{len(patterns)} permanent pattern(s), {len(timed)} timed:"]
    out += [f"  {pattern}" for pattern in patterns[:BANLIST_MAX]]
    out += [f"  {nick}  ({security.format_ban_duration(left)} left)" for nick, left in timed[:BANLIST_MAX]]
    _send_lines(session, out)
