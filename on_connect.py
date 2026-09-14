"""Commands to send to the server as soon as we are registered.

WHAT THIS IS FOR

Every network wants something different done before you are really "on" it.
Undernet wants you to log in to X and take `+x` so your host is hidden;
somewhere else it is NickServ, or a usermode, or a bot-flag your channel
insists on. None of that belongs hardcoded in the connect path, and all of it
has to happen at a very specific moment.

THE MOMENT MATTERS MORE THAN THE COMMANDS

These run after registration and BEFORE the JOIN. On Undernet that ordering is
not cosmetic: `+x` replaces your visible host, and joining first means your
real host is in the channel for everyone who was already there. Auth, then
mode, then join. irc.py's delayed_join() is where that sequence lives.

WHY A FILE OF ITS OWN

settings.conf is one `NAME = value` per line and deliberately refuses a value
that spans lines - an indented continuation used to join itself onto the
setting above, silently. A list of commands is exactly the shape that breaks
on, so it gets its own JSON file, the same way library_folders.json and
lists.json do.

THE FILE HOLDS A PASSWORD

An X login line contains one in plain text. Two consequences, both enforced
here rather than left to callers:

  * The command text is NEVER logged. Not to stdout, not to the debug channel -
    a debug channel is a CHANNEL, and printing an X password into one would
    hand it to everybody watching. Callers get a count.
  * `redacted()` is what any caller that must show something uses.
"""

import io
import json
import os
import re

import defaults as config
import platform_compat

# One IRC line is 512 bytes including the trailing CRLF. A command longer than
# that is not a command the server will ever see whole, so it is refused at the
# point it is written rather than truncated on the wire.
MAX_COMMAND_BYTES = 510
MAX_COMMANDS = 50
MAX_DELAY_SECONDS = 60
DEFAULT_DELAY_SECONDS = 2


def on_connect_file():
    """Where the commands live. Resolved per call, like every other path here:
    !rehash reloads config, and a path captured at import would keep pointing
    at the old location for the life of the process."""
    return getattr(config, "ON_CONNECT_FILE",
                   os.path.join("data", "on_connect.json"))


def _clean_command(raw):
    """One command, or "" if there is nothing usable in it.

    CR and LF come out rather than being refused. They are how one entry would
    become two commands on the wire, and an operator who pastes a block with a
    stray newline means one command - not one command and whatever the tail
    happens to parse as.
    """
    text = str(raw or "").replace("\r", " ").replace("\n", " ").strip()
    return text


def load(path=None):
    """(commands, delay_seconds). Empty list when there is nothing to send.

    Never raises. A file that cannot be read means "no on-connect commands",
    which is what every install has by default - it must not be able to stop
    the bot connecting.
    """
    target = on_connect_file() if path is None else path
    try:
        with io.open(platform_compat.long_path(target), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return [], DEFAULT_DELAY_SECONDS

    if not isinstance(raw, dict):
        return [], DEFAULT_DELAY_SECONDS

    commands = []
    for item in raw.get("commands") or []:
        text = _clean_command(item)
        if text:
            commands.append(text)

    try:
        delay = float(raw.get("delay_seconds", DEFAULT_DELAY_SECONDS))
    except (TypeError, ValueError):
        delay = DEFAULT_DELAY_SECONDS
    delay = max(0.0, min(float(MAX_DELAY_SECONDS), delay))

    return commands[:MAX_COMMANDS], delay


def problems(commands, delay_seconds, ignore_blanks=False):
    """Every reason this set could not be sent, as operator-readable lines.

    Returns [] when it is usable. Same shape and same reasoning as
    library.problems(): every fault at once, each naming the command it is
    about by POSITION rather than by text, because the text may be a password.
    """
    found = []

    if len(commands) > MAX_COMMANDS:
        found.append(f"{len(commands)} commands - at most {MAX_COMMANDS}.")

    for index, command in enumerate(commands, start=1):
        text = _clean_command(command)
        if not text:
            # save() strips blanks before storing them, so when it validates
            # they are not a fault - they are how somebody formats a pasted
            # block. The index still advances, which is the point: the number
            # has to match the line the operator is looking at.
            if not ignore_blanks:
                found.append(f"command {index}: blank.")
            continue
        # The NORMALIZED form, not the stored one - normalize() can grow a
        # line (PRIVMSG is longer than msg), and the 510-byte limit is about
        # what actually goes on the wire, not what the operator typed.
        wire = normalize(text)

        # A line that normalizes to nothing is new (#486): "/" on its own is
        # not blank to _clean_command(), but once the leading slash comes off
        # there is no command left, and what would reach the server is a bare
        # CRLF. Caught here rather than sent, because a stray empty line the
        # server discards is indistinguishable from a command that ran.
        if not wire.strip():
            found.append(f"command {index}: a slash and nothing else.")
            continue

        size = len(wire.encode("utf-8", "replace"))
        if size > MAX_COMMAND_BYTES:
            found.append(f"command {index}: {size} bytes, over the "
                         f"{MAX_COMMAND_BYTES}-byte IRC line limit. The server "
                         f"would never see all of it.")

    try:
        delay = float(delay_seconds)
    except (TypeError, ValueError):
        found.append(f"the delay must be a number of seconds.")
    else:
        if delay < 0 or delay > MAX_DELAY_SECONDS:
            found.append(f"the delay must be between 0 and "
                         f"{MAX_DELAY_SECONDS} seconds.")

    return found


def save(commands, delay_seconds, path=None):
    """Write them, refusing a set that could not be sent.

    Raises ValueError naming every problem at once. Written to a temporary
    file and renamed, like every other file this project writes: a half-written
    command list read at the next connect would send half a login.
    """
    import tempfile

    # VALIDATE THE ORIGINAL, STORE THE FILTERED. problems() reports a fault
    # by POSITION - deliberately, because the text may be a password and must
    # not be echoed back - so the number it gives is the only handle the
    # operator has for finding the line. Numbering the already-filtered list
    # made that number wrong by the count of preceding blank lines, and the
    # dashboard sends a textarea split with splitlines() and no filtering at
    # all: paste an X login, a blank separator, a MODE line and an over-long
    # one, and you are told "command 3" for what you typed on line 4.
    #
    # ignore_blanks because save() strips them on purpose - a blank line in a
    # pasted block is formatting, not a command.
    original = list(commands or [])
    cleaned = [_clean_command(c) for c in original]
    cleaned = [c for c in cleaned if c]

    found = problems(original, delay_seconds, ignore_blanks=True)
    if found:
        raise ValueError("\n".join(found))

    target = on_connect_file() if path is None else path
    directory = os.path.dirname(os.path.abspath(target)) or "."
    os.makedirs(platform_compat.long_path(directory), exist_ok=True)

    payload = json.dumps({"delay_seconds": float(delay_seconds),
                          "commands": cleaned},
                         indent=2, ensure_ascii=False)

    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=directory,
        prefix=".on-connect-", suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(payload + "\n")
        platform_compat.replace_with_retry(handle.name, target)
    except BaseException:
        try:
            os.remove(handle.name)
        except OSError:
            pass
        raise

    return cleaned


_MSG_SHORTHAND_RE = re.compile(r"^\s*msg\s+(\S+)\s+(.*)$", re.IGNORECASE)


def normalize(command):
    """Do to the line what a client does before it reaches the wire.

    The dashboard's instructions say to write these "exactly as you would
    type it into a client". A client does two things to a typed line, and
    this used to do neither.

    THE LEADING SLASH (#486)

    In a client you type `/msg`, not `msg`. The slash is how the client
    tells a command from something to say, and it is consumed there - it
    never goes on the wire. Sent verbatim it comes back
    `421 Unknown command`, which is the failure #474 was opened for, one
    keystroke away: that operator happened to omit the slash, and the X
    login instructions they were following tell them to include it.

    It was never a `msg` problem either. Every on-connect line had it -
    `/mode`, `/join`, `/nick` were all sent with the slash attached and all
    rejected.

    Exactly one slash comes off, whatever follows it, which also settles
    `//` (an escaped literal slash in most clients) by construction: it
    keeps one rather than losing both.

    THE MSG SHORTHAND (#474)

    `msg <target> <text>` is the one command whose client spelling is not
    its wire spelling - the wire wants `PRIVMSG <target> :<text>`. MODE,
    JOIN, NOTICE and everything else an on-connect script is likely to need
    are spelled the same in both places. PRIVMSG is the one mismatch, and
    the one nearly every X-login or NickServ instruction is written as.

    The slash comes off FIRST, so this catches `/msg` for nothing.
    """
    text = str(command or "")

    stripped = text.lstrip()
    if stripped.startswith("/"):
        text = stripped[1:]

    match = _MSG_SHORTHAND_RE.match(text)
    if not match:
        return text
    target, message = match.groups()
    return f"PRIVMSG {target} :{message}"


def expand(command, nickname=None):
    """Substitute the placeholders a command may use.

    Only `%nick%` so far, and it earns its place: `MODE %nick% +x` is the
    Undernet line, and the nick it needs is the one the SERVER gave us - which
    is not necessarily the one in the config, because a 433 collision rebinds
    it. Writing the configured nick into that command would send a MODE for
    somebody else.
    """
    nick = str(nickname if nickname is not None
               else getattr(config, "NICKNAME", "") or "")
    return str(command).replace("%nick%", nick)


def redacted(commands):
    """What a caller may show or log: the command word, and nothing after it.

    "PRIVMSG X@channels.undernet.org :LOGIN someone hunter2" becomes
    "PRIVMSG ...". The first word is enough for an operator to recognise which
    line ran, and everything that could be a password is in the rest.
    """
    shown = []
    for command in commands or []:
        first = str(command).strip().split(" ", 1)[0]
        shown.append(f"{first} ..." if first else "...")
    return shown
