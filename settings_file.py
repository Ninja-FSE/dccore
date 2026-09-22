# settings_file.py - reads settings.conf and applies it over config.py's defaults.
"""Plain-text settings, without turning config.py into a text file.

WHY THE DEFAULTS STAY IN PYTHON

The daemon targets Python 3.10, where the only stdlib config parser is
configparser - and configparser returns strings. tomllib, which carries real
types, arrived in 3.11. Adding a third-party TOML parser would spend this
project's "no third-party packages" property on a settings file.

So if the defaults lived in the text file too, something would have to declare
each value's type: 34 of the settings are not strings. That something is a
second list to keep in step with the first, and this codebase has already been
bitten by exactly that shape - PRESERVE_RUNTIME was a hand-maintained list of
names that drifted out of step with config.py and silently emptied live state.

Keeping the default as a Python literal makes the default ITSELF the type
declaration: `MAX_DCC_SLOTS = 5` is an int, so its override is read as an int.
There is nothing to maintain and nothing to drift.

The operator never edits Python: they edit settings.conf, and
settings.conf.sample lists every setting with its default and explanation.

NOTHING BREAKS FOR AN EXISTING INSTALL

admin_config.py still works exactly as it did. config.py applies it first and
then this file on top, so an operator who has one can ignore settings.conf
entirely, or migrate at their own pace, or use both. Where both set the same
name, settings.conf wins - it is the one the operator edited most recently by
definition, being the newer mechanism.

A BAD VALUE DOES NOT STOP THE DAEMON

An unreadable file, an unknown key, or a value that will not convert are all
reported loudly and then skipped, leaving the default in place. That matches
how the rest of this codebase treats its own edges - webserver.start() logs and
returns rather than raising, and the console encoding guard exists so that one
character cannot take the process down. A daemon that refuses to start at 3am
because of one typo in one setting is worse than one that starts and says
clearly which line it ignored.
"""

import configparser
import io
import os
import sys
import re
import platform_compat
import tempfile
import threading

# Where the file lives, unless DCCORE_SETTINGS_FILE points somewhere else.
# The environment variable exists for tests and for running two instances off
# one checkout; ordinary installs never set it.
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.conf")

# The settings a fresh install MUST change before oserve.startup() will boot -
# see unconfigured_required()'s own docstring for the mechanism, and issue
# #162 findings #18/#20 (the audit-followup RFC in #170's discussion)
# for why this exists at all: a bot that never touches these joins the
# upstream operator's live channels under his nickname, reports its debug
# output into his channel, and grants HIS admin nicks full control.
#
# One list, feeding three consumers, so they cannot drift apart the way
# PRESERVE_RUNTIME and the two hand-maintained check-setup.py copies both
# did before this codebase learned that lesson twice already:
#   - scripts/gen_settings_sample.py renders these blank in settings.conf.sample
#     instead of showing the real shipped value, so there is nothing to
#     accidentally copy-paste-and-keep.
#   - oserve.startup() refuses to boot (sys.exit(1)) while any of these still
#     resolves to its shipped default, or is blank - see
#     unconfigured_required() below.
#   - scripts/setup_check.py's pre-flight report (run before the first real
#     start) surfaces the same blank/still-default state as an EARLIER,
#     friendlier warning - a full diagnostic report, not just a refusal -
#     reading this same REQUIRED set, not a second, separately maintained
#     one that could disagree about WHICH settings matter.
#
# LIST_BASE_NAME is deliberately not here even though setup_check.py has
# historically cared about it too - #170's RFC comment notes it
# "can derive from NICKNAME rather than being asked for at all" once NICKNAME
# itself is required; defaults.py's own "DERIVED VALUES" section now does
# exactly that (an untouched LIST_BASE_NAME takes NICKNAME's value once
# NICKNAME is set), so forcing it blank here too would just be a second,
# redundant way of demanding the same answer.
#
# SERVER and DEBUG_CHANNEL are deliberately NOT here, even though the RFC
# discussion's own first pass included them. That was found during a real
# test-run against the live install: the gate below refuses a
# name whose CURRENT value equals its SHIPPED default - correct for an
# identity setting (leaving NICKNAME/CHANNEL/ADMIN_NICK at their shipped
# values means impersonating the upstream operator), but "irc.undernet.org"
# is not somebody else's identity to avoid - it is the correct server for
# essentially every operator of an Undernet file server. DEBUG_CHANNEL is out
# for a different reason since it started shipping blank: an install with no
# debug channel is not misconfigured, it simply has no debug channel, and
# irc.py skips the JOIN. Keeping SERVER REQUIRED would make its own correct,
# intended value permanently unusable: an operator who explicitly writes
# SERVER = "irc.undernet.org" is refused for exactly the same reason as one
# who never touched it at all, because the gate cannot tell those two apart
# by value alone. Dropping both from REQUIRED - rather than special-casing
# the comparison for just these two - keeps the mechanism one simple rule
# ("blank means never configured") instead of a rule with exceptions.
#
# FILE_DIRECTORY is ALSO not here, even though an earlier version of this
# set included it - found live, running configure.py against a real install:
# requiring it here meant the daemon could not boot at all without a music
# directory chosen up front, which is the one thing this project's own web
# dashboard is a genuinely easier place to set (browse-and-confirm, rather
# than typing a path blind at a prompt) - but the dashboard needs the daemon
# RUNNING to reach it at all, so requiring FILE_DIRECTORY here made the
# dashboard's own Settings page unable to be the place that sets it for a
# fresh install. oserve.startup() still refuses to start with a FILE_
# DIRECTORY that is set but does not exist (a real misconfiguration, worth
# catching hard) - only "not chosen yet" no longer blocks booting at all.
REQUIRED = frozenset({
    "NICKNAME", "CHANNEL", "ADMIN_NICK",
})


def unconfigured_required(namespace, shipped_defaults):
    """Which names in REQUIRED are still exactly their shipped default, or
    blank, in `namespace` - i.e. genuinely never overridden by this install.

    `shipped_defaults` is config.SHIPPED_DEFAULTS: a snapshot config.py takes
    of its own REQUIRED literals BEFORE admin_config.py or settings.conf ever
    apply. Comparing against that snapshot, not against config.py's source
    freshly re-read, is what lets a rehash's importlib.reload(config)
    re-execute the same snapshot line and get the identical values back
    every time - there is exactly one place these can be defined, so it can
    never itself drift out of step with config.py.

    A name is reported as unconfigured when its CURRENT value (after both
    override mechanisms have already applied) is blank, or is identical to
    what config.py shipped before either one ran. An install that set a
    REQUIRED name to something new - even something that happens to collide
    with another operator's own choice - is not flagged: this only catches
    "never touched it", not "touched it to something someone else also
    picked".

    Returns names in REQUIRED's own sorted order, for a stable, readable
    startup message.
    """
    unconfigured = []
    for name in sorted(REQUIRED):
        current = namespace.get(name)
        if not str(current or "").strip():
            unconfigured.append(name)
            continue
        if name in shipped_defaults and current == shipped_defaults[name]:
            unconfigured.append(name)
    return unconfigured

# Guards save()'s read-modify-write cycle: it reads the existing file,
# computes the edited text, then atomically replaces it - but two overlapping
# calls would each read the same starting file and each write back a version
# containing only their own change, silently losing whichever one lost the
# race. Lives here rather than in each caller, same as db.py's own
# _disk_lock: the invariant ("no two saves interleave") belongs to the
# function doing the read-modify-write, not to whichever module happens to
# call it first - a second caller (a setup-mode writer, an adminchat command)
# should not have to know this lock exists to be safe.
_save_lock = threading.Lock()

# configparser insists on sections. The file is allowed to use them purely to
# group things for a human reader - they are flattened away on read, so no
# name-to-section map has to be kept in step with config.py.
_SYNTHETIC_SECTION = "__dccore__"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# Settings whose value is one of a fixed few, rather than free text. A plain
# string field takes anything, and "ZIP" or "tar" would be written happily,
# read back unchanged, and then match nothing at the point of use - the bot
# would go on running and quietly stop serving a list. Naming the choices here
# turns that into a refusal at the point of saving, with the reason, and into a
# "kept the default" line in the startup log for a hand-edited settings.conf.
#
# Case is not part of the choice: an operator typing "ZIP" means "zip".
#
# THEME is the same shape as LIST_FORMAT - a fixed few names, not free text -
# but theme.THEMES is not imported here to build this tuple. theme.py does
# `import defaults as config`, and defaults.py does `import settings_file` at
# module scope (to apply admin_config.py/settings.conf on top of its own
# declarations) - so `import theme` here closes that into a cycle. It does
# not always fail: whichever of settings_file/defaults happens to be
# imported FIRST anywhere in the process decides whether Python resolves it
# from a module already fully built or from one still mid-import, so the
# failure would depend on import order rather than on anything about this
# file. Verified by trying it: importing defaults before settings_file
# happened to work, importing settings_file first raised
# "partially initialized module 'settings_file' has no attribute 'apply_to'"
# - the same values, opposite failure depending on which module a test or
# entry point happens to touch first. Named here instead, same as
# LIST_FORMAT; tests/test_theme.py pins it against theme.THEMES so the two
# cannot drift silently if a preset is ever added or renamed.
CHOICES = {
    "LIST_FORMAT": ("txt", "zip", "rar"),
    "THEME": ("classic", "midnight", "forest", "orchid", "plain"),
    # adminchat.handle_dcc_chat() tests for "listen" and "connect" and treats
    # everything else as "auto", so a typo did not fail - it silently selected
    # the default. The operator who wrote "lisen" got automatic mode and no
    # indication their setting had not taken. Found by audit.
    "ADMIN_CHAT_MODE": ("auto", "listen", "connect"),
    # mIRC's own packet-size menu, in bytes: 4, 8, 16, 32, 64, 128 KB. A list
    # rather than a free number because that is what an operator is comparing
    # against - "the same setting mIRC has" - and because the six of them are
    # the only values anyone actually wants to try.
    "DCC_BLOCK_SIZE": ("4096", "8192", "16384", "32768", "65536", "131072"),
}


class SettingsError(Exception):
    """The file could not be parsed at all. Individual bad values do not
    raise - they are reported and skipped."""


def settings_path():
    return os.environ.get("DCCORE_SETTINGS_FILE") or DEFAULT_PATH


# Uppercase names that describe the CODE rather than the installation.
#
# SCRIPT_VERSION was an editable field on the dashboard's Settings page. Saving
# it wrote the number into settings.conf, and settings.conf is applied AFTER
# the shipped defaults - so from that moment the value in the file won, for
# ever. The next upgrade shipped a new version and the daemon went on
# reporting the old one, in the advert, the list masthead, the CTCP VERSION
# reply and the dashboard's own header, with nothing to say why. "What version
# are you running?" is the first question asked about any install, and this
# made the answer unreliable in the one direction nobody checks.
#
# PROJECT_URL is deliberately NOT here: a fork pointing at its own repository
# is a real configuration, and the rule above already stops it being blanked.
NOT_SETTINGS = frozenset({"SCRIPT_VERSION"})


# NAMES THE DAEMON ASSIGNS TO ITSELF WHILE IT RUNS (#465).
#
# Neither is declared in config.py, so both land in apply_to()'s `unknown`
# list and are ignored - correctly. What was wrong is what the operator was
# then told: "not a setting this version recognises. Check the spelling"
# sends somebody to check a name that is spelled perfectly and is simply not
# set from this file. MY_IP_OR_DOCK in particular is named by two runtime
# messages as something to configure, so an operator reaching this line has
# usually just been told to set it.
#
# A MESSAGE, NOT A GATE. Nothing here makes either name applicable, and
# _check_writable() still refuses to WRITE one for the reason in its own
# comment: MY_IP_OR_DOCK is the address DETECTED at startup, and a value in
# the file would freeze one session's answer into every session after it.
RUNTIME_ASSIGNED = {
    "MY_IP_OR_DOCK": ("the daemon detects this address at startup rather than "
                      "reading it from a file. Set it in admin_config.py if "
                      "you need to pin it"),
    "ORIGINAL_NICK": ("the daemon remembers this for itself, from NICKNAME, so "
                      "it can go back to it after a nick collision"),
    # NOT_SETTINGS' one member (#688, audit L24): an older dashboard's
    # Settings page offered it and wrote it here, nothing ever removed the
    # line, and every boot and !rehash since said "check the spelling" of
    # a name DCCore itself had written.
    "SCRIPT_VERSION": ("the code's own version, which an older Settings page "
                       "wrote here; it is no longer configurable. Delete this "
                       "line"),
}


def is_overridable(name, value):
    """Is `name` something an operator may set in settings.conf?

    Settings are the uppercase names. The C_* colour codes are excluded: they
    are raw mIRC control bytes (`\\x0303`), protocol constants rather than
    preferences, and expressing them in a text file would mean typing escape
    sequences. A named theme setting is the right shape for that, if themes
    ever land - a theme name is an ordinary string.
    """
    if not name.isupper() or name.startswith("_"):
        return False
    if name.startswith("C_"):
        return False
    if name in NOT_SETTINGS:
        return False
    return isinstance(value, (str, bool, int, float, list, type(None)))


def parse(text):
    """Return {KEY: raw string} for every entry, sections flattened.

    Raises SettingsError if the text is not parseable, or if one key appears
    twice - silently keeping one of two conflicting values for the same
    setting is exactly the kind of quiet wrongness this file exists to avoid.
    """
    # interpolation=None: configparser otherwise treats '%' as a reference and
    # a Windows path or a password containing one fails in a way that reads
    # like a corrupt file.
    # An INDENTED line is a continuation to configparser, and silently becomes
    # part of the previous value - newline and all. settings.conf is one
    # setting per line (save() refuses to write a value containing a line
    # break for exactly that reason), so an indented line is always a mistake,
    # and the mistake was invisible:
    #
    #     NICKNAME = MyBot
    #         a stray indented line
    #
    # gave NICKNAME the value "MyBot\na stray indented line", which then went
    # out on the wire. Found by audit. Refused here, naming the line, rather
    # than left to be discovered as a nickname the server rejects.
    #
    # Indented COMMENTS are still fine - they are comments wherever they sit.
    for number, raw_line in enumerate(text.split("\n"), start=1):
        if not raw_line[:1] in (" ", "\t"):
            continue
        stripped = raw_line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        raise SettingsError(
            f"line {number} is indented: {stripped[:60]!r}. Every setting "
            f"starts at the beginning of its own line - an indented line is "
            f"read as a continuation of the setting above it, which is almost "
            f"never what was meant.")

    parser = configparser.ConfigParser(interpolation=None)
    # Keep the case of keys. Settings are uppercase; the default optionxform
    # lowercases everything and nothing would ever match.
    parser.optionxform = str

    try:
        parser.read_string(f"[{_SYNTHETIC_SECTION}]\n" + text)
    except configparser.Error as err:
        raise SettingsError(str(err)) from err

    flat = {}
    seen_in = {}
    for section in parser.sections():
        for key, raw in parser.items(section):
            upper = key.upper()
            if upper in flat:
                raise SettingsError(
                    f"{key!r} is set twice - once in [{seen_in[upper]}] and "
                    f"again in [{section}]. Remove one; there is no way to "
                    f"tell which was meant.")
            flat[upper] = raw
            seen_in[upper] = section
    return flat


def declared_types(namespace):
    """{NAME: type} for every setting config.py annotates.

    A module's annotated assignments populate its `__annotations__`, so this
    reads config.py's own declaration back rather than being a second list to
    keep in step with it - the same reason the default value itself was the
    type declaration before annotations existed.

    Anything whose annotation is not a plain type is ignored rather than
    guessed at, and the caller falls back to the default's own type.

    PYTHON 3.14 DOES NOT PUT THEM IN `__dict__`. PEP 649 made annotations
    lazy: a module grows an `__annotate__` function, and `__annotations__` is
    computed the first time the ATTRIBUTE is read. `vars(module)` hands back
    the raw `__dict__`, which does not contain it until then - so this
    returned {} on 3.14 while `config.__annotations__` held all 94.

    Nothing raised. Every caller simply saw a config with no declared
    settings: the dashboard's Settings page rendered zero fields in every
    category, and both the reader and the writer fell back to the default's
    runtime type - which for `WEBUI_CONSOLE_ENABLED: bool = None` is
    NoneType, so the value came through as raw text rather than a bool.

    Found by running the daemon on 3.14 during the RC1 beta. CI covers 3.10
    and 3.12, and the README promises "3.10+", which now includes this.
    """
    annotations = namespace.get("__annotations__")
    if annotations is None:
        # Read it as an ATTRIBUTE, which is what triggers the lazy build. The
        # module is found by its own __name__ rather than being passed in, so
        # every existing caller keeps handing us vars(config) unchanged.
        module = sys.modules.get(str(namespace.get("__name__", "")))
        annotations = getattr(module, "__annotations__", None)
    return {name: kind for name, kind in (annotations or {}).items()
            if isinstance(kind, type)}


_IRC_ESCAPE_RE = re.compile(r"\\x([0-9A-Fa-f]{2})")


def decode_irc_escapes(text):
    r"""Turn the `\xHH` sequences an operator can TYPE into the bytes mIRC
    actually reads.

    settings.conf.sample documents each CUSTOM_THEME_* override as "a raw mIRC
    code string like \x0306,06", and defaults.py says the same. Neither was
    true: coerce() returned the text verbatim, so the nine literal characters
    went into every advert, every "Sent:" notice and every search header,
    broadcast on a five-minute cycle with no error anywhere.

    Nor could the operator work around it. A colour code starts with 0x03,
    which is a control character: settings.conf is a text file they edit in an
    editor, and the dashboard field is a browser text input that cannot
    produce that byte at all. Typing the escape was the only route there was,
    and it was the one route that did not work.

    ONLY \xHH, deliberately. That covers every code mIRC uses - 0x03 colour,
    0x02 bold, 0x1F underline, 0x0F reset - and leaves every other backslash
    alone, so a Windows path in some future string setting is not quietly
    mangled by a decoder that was only ever meant for control codes. A literal
    backslash-x-digit-digit is therefore not expressible here, which no theme
    string wants.
    """
    return _IRC_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 16)),
                              str(text))


_IRC_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# #436: a real theme role is a handful of mIRC control codes - "\x0302,02" is
# 8 bytes, the longest in the shipped THEMES table - so this leaves an
# operator plenty of room to combine a couple of codes while still bounding
# how much one role can eat out of announce.IRC_LINE_BUDGET when it is
# interpolated 8-9 times into a single line.
CUSTOM_THEME_MAX_BYTES = 30


def encode_irc_escapes(text):
    r"""The way back out: control bytes as the `\xHH` an operator can read.

    decode_irc_escapes() has been half a round trip. It turns typed `\x0313`
    into the byte mIRC reads, and nothing turned it back - so a colour that
    had been through config once was a raw 0x03 from then on, and 0x03 is not
    a character a text file or a browser input can show. The dashboard field
    for an accent of `\x0313` read `13`: the code was there, invisible, and
    what the operator could see was not what was set. Typing back what they
    read would have put a literal "13" into every advert.

    It also decides what lands in settings.conf, which is a file people edit
    by hand. A raw control byte in it is invisible in an editor and is exactly
    the kind of thing an editor strips on save.

    EVERY control character, not just the ones a theme uses. The set that
    cannot survive a text file is the set below, and picking a shorter one
    would mean deciding today which codes a future theme may want.

    Lowercase hex, so a value has ONE spelling once it has been through here -
    the decoder accepts either case, and two spellings of the same colour
    would each read as an edit of the other. Idempotent on text that is
    already escaped, since that text holds no control characters at all.
    """
    return _IRC_CONTROL_RE.sub(
        lambda match: "\\x%02x" % ord(match.group()), str(text))


# WHAT AN IRC NICKNAME MAY BE (#591). The server decides, and the protocol
# (RFC 2812, and ircu on Undernet) says: ASCII, a letter or one of [ ] \\ ` _ ^ { | }
# first, then those plus digits and "-". Anything else - a Greek letter, "!",
# ".", "*", a comma, a leading "-" or digit, a space - is answered with 432
# "Erroneous Nickname", which the handshake treats like 433 "in use": the bot
# quietly ran as the shipped alternate ("DCCore_") and reported the nick as
# "taken". Every path that writes a nickname comes through here, so the operator
# hears it when they type it.
NICK_SETTINGS = frozenset({"NICKNAME", "ALT_NICKNAME", "ADMIN_NICK"})
_NICK_FIRST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz[]\\`_^{|}"
_NICK_REST = _NICK_FIRST + "0123456789-"


def nick_problem(nick):
    """Why `nick` cannot be an IRC nickname, or None if it can."""
    text = str(nick)
    if not text:
        return "a nickname cannot be empty"
    if "\n" in text or "\r" in text:
        return f"{text!r} has a line break in it"
    for character in text:
        if character == " ":
            return f"{text!r} has a space in it"
        if ord(character) > 127:
            return (f"{text!r} has the character {character!r}, which is not ASCII - "
                    "IRC servers refuse it")
    if text[0] not in _NICK_FIRST:
        return f"{text!r} starts with {text[0]!r}; a nickname starts with a letter or one of [ ] \\ ` _ ^ {{ | }}"
    for character in text[1:]:
        if character not in _NICK_REST:
            return f"{text!r} has {character!r} in it; a nickname has letters, digits and [ ] \\ ` _ ^ {{ | }} -"
    return None


def server_problem(value):
    """Why `value` cannot be SERVER - a host name the bot can resolve - or
    None if it can (#687, audit L23).

    The natural first-timer spellings "irc.undernet.org:6667" and a pasted
    "irc://irc.undernet.org" were accepted, and connect() then failed on
    name resolution every ten seconds for ever - "[ERROR] Connection failed:
    [Errno 11001] getaddrinfo failed" - with nothing saying the colon or the
    scheme was the problem. The port has its own setting (PORT), and a host
    name has no ":", "/" or spaces in it.
    """
    text = str(value).strip()
    if not text:
        return "a server name cannot be empty"
    if "://" in text:
        return (f"{text!r} is a URL; SERVER is the host name alone, "
                f"e.g. {text.split('://', 1)[1].split('/', 1)[0].split(':', 1)[0] or 'irc.undernet.org'}")
    if "/" in text:
        return f"{text!r} has a / in it; SERVER is the host name alone"
    if ":" in text:
        host, _colon, port = text.partition(":")
        if port.isdigit():
            return f"{text!r} carries the port; put {host!r} in SERVER and {port} in PORT"
        return f"{text!r} has a : in it; SERVER is the host name alone, and PORT is its own setting"
    if " " in text:
        return f"{text!r} has a space in it"
    return None


def admin_host_problem(value):
    """Why `value` cannot be the host half of an ADMIN_HOSTMASKS entry, or
    None if it can (#811, follow-up to audit M52/H4, #654/#579).

    Setup asks for the host alone - what `/whois yourself` shows once you
    are logged into services and set +x, e.g. "yourname.users.undernet.org"
    - and wraps it as "*!*@<value>" itself; this checks the part the
    operator actually typed. A "*" is refused outright: a real host from
    /whois never has one, and the one way to end up with one here is
    pasting the wildcard-breadth mistake audit L5 (#669) warns about
    instead of an actual host - refusing it at the source is cheaper than
    warning about it after it is written.
    """
    text = str(value).strip()
    if not text:
        return "a host cannot be empty"
    if "://" in text:
        return f"{text!r} looks like a URL; give the host alone, e.g. yourname.users.undernet.org"
    for character in " /@!*":
        if character in text:
            return f"{text!r} has a {character!r} in it; give the host alone, without the nick, ident or a wildcard"
    if "." not in text:
        return f"{text!r} does not look like a host - it should end in something like .users.undernet.org"
    return None


def nicks_problem(value):
    """The same for a comma-separated list (ADMIN_NICK)."""
    parts = [part.strip() for part in str(value).split(",")]
    if not any(parts):
        return "a nickname cannot be empty"
    for part in parts:
        if not part:
            continue
        problem = nick_problem(part)
        if problem:
            return problem
    return None


def coerce(name, raw, default, declared=None):
    """Convert `raw` to `declared`, or to the type of `default` without one.

    Raises ValueError on failure.

    The declared type is what lets a setting be unset-until-configured. A
    default of None says nothing about what the value should become, so
    `RAR_BINARY: str = None` is the annotation carrying that meaning instead
    of the value having to imply it.
    """
    text = raw.strip()
    kind = declared if declared is not None else type(default)

    # Before any type conversion: a choice setting is only ever one of a few
    # strings, and the point of checking here is that every path into config -
    # settings.conf at startup, the settings form, apply_settings_changes -
    # comes through coerce().
    if name in CHOICES:
        lowered = text.lower()
        if lowered not in CHOICES[name]:
            raise ValueError(
                f"expected one of {list(CHOICES[name])}, got {raw!r}")
        # A NUMERIC choice still comes back as a number. Every choice was a
        # string until DCC_BLOCK_SIZE, and returning "65536" for a setting
        # declared `int` would hand the send loop a str to read() with - the
        # kind of type drift that only shows up on the one code path nobody
        # exercised. The declared default decides, exactly as it does for
        # every non-choice setting below.
        #
        # `is not True/False` rather than `not isinstance(default, bool)`
        # would be the same thing here, but bool IS an int in Python and a
        # future True/False choice must not be turned into 1/0.
        if isinstance(default, bool):
            return lowered
        if isinstance(default, int):
            return int(lowered)
        return lowered

    if name in NICK_SETTINGS and text:
        problem = nicks_problem(text) if name == "ADMIN_NICK" else nick_problem(text)
        if problem:
            raise ValueError(f"not a valid IRC nickname: {problem}")

    if name == "SERVER" and text:
        problem = server_problem(text)
        if problem:
            raise ValueError(f"not a server name: {problem}")

    if default is None and not text:
        # A setting whose default is None is "unset unless you say otherwise"
        # - RAR_BINARY is the example, meaning "look on PATH". An empty value
        # in the file means the same thing rather than an empty string.
        return None

    # bool is checked BEFORE int for readers who remember why it had to be:
    # bool is a SUBCLASS of int, so the isinstance() test this used to do
    # matched True and False as well and read every flag as a number. Matching
    # the type exactly removes that trap rather than ordering around it.
    if kind is bool:
        lowered = text.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueError(
            f"expected a yes/no value (one of {sorted(_TRUE | _FALSE)}), got {raw!r}")

    if kind is int:
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"expected a whole number, got {raw!r}") from None

    if kind is float:
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"expected a number, got {raw!r}") from None

    if kind is list:
        return [part.strip() for part in text.split(",") if part.strip()]

    if kind is type(None):
        # No annotation and a None default: nothing declares what this should
        # become, so it stays the text the operator wrote.
        return text

    if name.startswith("CUSTOM_THEME_"):
        return decode_irc_escapes(text)

    return raw.strip("\n")


def apply_to(namespace, path=None, log=print):
    """Read the settings file and write recognised values into `namespace`.

    `namespace` is config.py's globals(). Returns a report dict; nothing here
    raises for ordinary problems, so a caller can ignore the return value and
    still get a correct daemon with the defaults intact.
    """
    path = path or settings_path()
    report = {"path": path, "applied": {}, "unknown": [], "bad": [], "shadowed": [],
              "read_error": None}

    if not os.path.exists(path):
        return report

    try:
        # "utf-8-sig", not "utf-8" (#446). A byte-order mark is three bytes
        # that decode to U+FEFF and render as nothing at all. Notepad writes
        # one by default, and read as plain utf-8 it stays attached to the
        # first line - so the FIRST setting in the file parses as
        # "\ufeffMSG_DELAY", lands in `unknown`, and is silently ignored while
        # the operator looks at a file that plainly sets it. utf-8-sig
        # consumes a BOM when present and is identical to utf-8 when not.
        with io.open(path, encoding="utf-8-sig") as handle:
            entries = parse(handle.read())
    except (OSError, UnicodeDecodeError, SettingsError) as err:
        report["read_error"] = str(err)
        log(f"[CONFIG] Could not read {os.path.basename(path)}, "
            f"continuing with the built-in defaults: {err}")
        return report

    types = declared_types(namespace)
    for key, raw in entries.items():
        if key not in namespace or not is_overridable(key, namespace[key]):
            report["unknown"].append(key)
            continue
        try:
            value = coerce(key, raw, namespace[key], types.get(key))
        except ValueError as err:
            report["bad"].append((key, str(err)))
            continue
        namespace[key] = value
        report["applied"][key] = value

    report["shadowed"] = _overridden_admin_config_values(report["applied"])
    _log_summary(report, path, log)
    return report


def _overridden_admin_config_values(applied):
    """[(name, admin_config's value)] for each name this file just applied
    that admin_config.py had ALSO set - to something else.

    The only other shadow check, shadowed_by_admin_config(), runs when the
    dashboard SAVES a setting. Nothing said anything when the daemon
    STARTED, which is when the collision actually bites: an operator who
    edits WEBUI_HOST in admin_config.py (its own comment tells them to) and
    restarts gets a dashboard that ignores the edit and a console that
    says nothing about why (#623).

    sys.modules rather than `import admin_config`: what matters is the
    module THIS process applied, `from admin_config import *` in defaults.py
    a moment before this runs. A process that never imported it (no file at
    boot, or a caller that is not defaults.py) has nothing to compare
    against, and importing one from disk here would report a file the
    daemon did not read.

    Only a DIFFERENT value counts. The same value in both files loses
    nothing - and a warning that fires on every boot for a harmless line
    is a warning nobody reads by the time it matters.
    """
    module = sys.modules.get("admin_config")
    if module is None:
        return []
    return sorted(((key, getattr(module, key)) for key, value in applied.items()
                   if hasattr(module, key) and getattr(module, key) != value),
                  key=lambda item: item[0])


def _log_summary(report, path, log):
    name = os.path.basename(path)
    if report["applied"]:
        log(f"[CONFIG] Applied {len(report['applied'])} setting(s) from {name}.")
    for key, admin_value in report["shadowed"]:
        log(f"[CONFIG] {name} overrides {key}, which admin_config.py also sets "
            f"(to {admin_value!r}): {name} is applied second and wins. Change "
            f"it in {name} or the dashboard's Settings page, or remove the "
            f"line from admin_config.py.")
    for key in report["unknown"]:
        assigned = RUNTIME_ASSIGNED.get(key)
        if assigned:
            log(f"[CONFIG] {name}: ignoring {key!r} - {assigned}.")
            continue
        log(f"[CONFIG] {name}: ignoring {key!r} - not a setting this version "
            f"recognises. Check the spelling against settings.conf.sample.")
    for key, why in report["bad"]:
        log(f"[CONFIG] {name}: ignoring {key} - {why}. Keeping the default.")


# ---------------------------------------------------------------------------
# Writing the file back
# ---------------------------------------------------------------------------
#
# Everything above reads. save() exists so the web dashboard can offer a
# settings page instead of asking an operator to edit a file by hand, and
# because config.py ends with apply_to(globals()), a save followed by the
# rehash the admin console already has picks the new values up live.
#
# THE FILE IS THE OPERATOR'S, NOT OURS
#
# settings.conf starts life as a copy of settings.conf.sample: every setting
# present, commented out, under section headers, with the explanation for each
# one above it. An operator uncomments the few they care about and often adds
# notes of their own. A writer that rebuilt the file from a dict of values
# would throw all of that away on the first save.
#
# So this edits lines rather than rewriting files. A setting already present
# has its value replaced in place; a setting present only as its commented-out
# default is uncommented where it stands, keeping the explanation above it;
# and only a setting that appears nowhere at all is appended. Every other byte
# of the file is left exactly as it was.
#
# ONE MISTAKE HERE BREAKS EVERY SETTING, NOT ONE
#
# parse() refuses a file where a key appears twice, on purpose - silently
# keeping one of two conflicting values is the quiet wrongness this module
# exists to avoid. But that refusal is all-or-nothing: it raises for the whole
# file, apply_to() catches it, and the daemon starts with every setting back at
# its default. So a writer that appended a key already present would not break
# that setting, it would break all fifty-one of them, at the next restart,
# with nothing to connect the two events.
#
# That is why nothing here is written on the strength of having got the edit
# right. The finished text is parsed and coerced BEFORE it reaches the disk,
# and unless it reads back as exactly the values that were asked for, the file
# on disk is not touched at all.

_ASSIGNMENT_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<comment>#[ \t]*)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<gap>[ \t]*)(?P<sep>[=:])(?P<rest>.*)$")


class SettingsWriteError(Exception):
    """The file was not written, and the reason why.

    Raised before anything reaches the disk. A caller that sees this knows the
    file on disk is exactly as it was.
    """


def render(value):
    """A Python value as the text settings.conf would carry for it.

    The same shapes gen_settings_sample.py writes, so a value saved here and a
    default shown in the sample look alike.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(part) for part in value)
    if value is None:
        return ""
    return str(value)


def _check_writable(name, value, namespace, types):
    """The text to write for `value`, and the value it will read back as.

    Returns (text, value). Refuses, with the reason, anything that would be
    written happily and read back as something else - which is worse than
    refusing, because the operator would see it saved and the daemon would run
    with something different.
    """
    if name not in namespace or not is_overridable(name, namespace[name]):
        raise SettingsWriteError(
            f"{name!r} is not a setting this version recognises, so writing it "
            f"would put a line in the file that nothing ever reads.")

    # config.py's annotation is what makes a name a SETTING rather than just an
    # uppercase attribute. The daemon and the test harness both assign names to
    # config at runtime - ORIGINAL_NICK, MY_IP_OR_DOCK - which look exactly like
    # settings from vars(config) and are not: MY_IP_OR_DOCK is the address
    # detected at startup, and writing it into the file would freeze one
    # session's answer in place for every session after it. apply_to() would
    # then dutifully apply it, and the detection would never run again.
    if name not in types:
        raise SettingsWriteError(
            f"{name} is not declared as a setting in config.py, so it is "
            f"something the daemon sets while it runs rather than something an "
            f"operator configures. Writing it would make one run's value "
            f"permanent.")

    # A web form sends strings for everything, including the 34 settings that
    # are not strings. A string is therefore taken as the text an operator
    # typed and read the same way the file would read it - which for a setting
    # that IS a string returns it unchanged, so a caller holding real Python
    # values can pass those instead and both callers get the same answer.
    if isinstance(value, str):
        try:
            value = coerce(name, value, namespace[name], types.get(name))
        except ValueError as err:
            raise SettingsWriteError(f"{name}: {err}") from None

    # A REQUIRED setting cannot be blanked. oserve.startup() already refuses to
    # boot while NICKNAME, CHANNEL or ADMIN_NICK is empty - but that check runs
    # at BOOT, and the two places these are written from are the web
    # dashboard's Settings page and configure.py. Saving a blank one was accepted,
    # reported as saved, and the daemon then refused to start.
    #
    # The dashboard goes down with the daemon, so the one screen the value
    # could be corrected from is gone too, and nothing anywhere says that
    # hand-editing settings.conf is now the only way back. Refusing the write
    # is the difference between an error message and an unbootable install.
    if name in REQUIRED and not str(value).strip():
        raise SettingsWriteError(
            f"{name} cannot be blank - the daemon refuses to start without it. "
            f"Clearing it here would leave no way to set it again except by "
            f"editing settings.conf by hand.")

    # Nor can a setting the daemon has no blank behaviour FOR, which REQUIRED
    # does not cover: it holds the three an operator must supply, not the many
    # that already work and simply cannot be empty.
    #
    # The shipped default answers it without a list to maintain. A default of
    # None means "unset unless you say otherwise", and blank is how an
    # operator says it again - RAR_BINARY cleared is "look on PATH", and
    # DEBUG_CHANNEL ships blank precisely because no debug channel is a real
    # configuration. A non-empty shipped default is the opposite: there is no
    # code path for empty anywhere.
    #
    # Found by a sweep of every string field the dashboard offers. Four were
    # accepted blank and each is used verbatim on the wire or on disk -
    # SERVER as the connect() host, ALT_NICKNAME as "NICK <alt>" after a 433,
    # LIST_BASE_NAME as the master list's filename, WEBUI_HOST as the
    # interface the dashboard binds, where empty means EVERY interface rather
    # than the loopback the default is careful to specify.
    #
    # The SHIPPED default, not the current one: after a first blank save the
    # current value IS empty, and a rule reading that would let every save
    # after it through.
    shipped = (namespace.get("SHIPPED_VALUES") or {}).get(name)
    if (isinstance(shipped, str) and shipped.strip()
            and not str(value).strip()):
        raise SettingsWriteError(
            f"{name} cannot be blank - it ships as {shipped!r} and the daemon "
            f"has no behaviour for an empty one. Set it to the value you want "
            f"instead of clearing it.")

    text = render(value)

    # A COLOUR GOES BACK OUT THE WAY IT CAME IN. coerce() above has just
    # decoded "\\x0313" into the byte mIRC reads, and writing that byte is
    # what put a raw 0x03 into settings.conf - a file people edit by hand,
    # where it is invisible in an editor and is exactly what an editor strips
    # on save. decode_irc_escapes() was half a round trip; this is the half
    # that was missing.
    #
    # #436: the ENCODED form is what the line-break check below is blind
    # to - an escaped "\x0a" holds no line break at all, so
    # decode_irc_escapes() (already run by coerce() above) is the last
    # point that ever sees the real byte before it goes back into hiding.
    # theme.blocks() interpolates a CUSTOM_THEME_* value 8-9 times into a
    # single outbound line, so a real \n or \r here - typed as the
    # documented \x0a/\x0d escape, which is otherwise perfectly legal -
    # turned one advert into nine separate IRC commands sent in a single
    # send(), reserving exactly one runtime.outbound_pacer slot for all
    # nine: an Excess Flood disconnect on every reconnect, with nothing in
    # the log naming the setting. \x00 is refused for the same reason
    # settings.conf itself cannot carry it. The length cap exists because
    # 8-9 repeats of one role compete with everything else for
    # announce.IRC_LINE_BUDGET.
    if name.startswith("CUSTOM_THEME_") and text:
        if any(ch in text for ch in ("\x00", "\x0a", "\x0d")):
            raise SettingsWriteError(
                f"{name}: cannot contain a newline or a null byte, even "
                f"written as the \\x0a/\\x0d escape - it would be "
                f"interpolated into every outbound line as a real one.")
        encoded_len = len(text.encode("utf-8", "ignore"))
        if encoded_len > CUSTOM_THEME_MAX_BYTES:
            raise SettingsWriteError(
                f"{name}: too long ({encoded_len} bytes, max "
                f"{CUSTOM_THEME_MAX_BYTES}) - this is interpolated 8-9 "
                f"times into a single outbound line, so a long value can "
                f"push an ordinary advert over the IRC line budget.")

    # BEFORE the checks below, not after, so they weigh what will actually be
    # written. The line-break check in particular: an escaped value holds no
    # line break at all, so a control character that would have been refused
    # is now simply written in the form the file can carry.
    if name.startswith("CUSTOM_THEME_"):
        text = encode_irc_escapes(text)

    if "\n" in text or "\r" in text:
        raise SettingsWriteError(
            f"{name}: a value cannot contain a line break - the file is one "
            f"setting per line, and the rest would be read as a new setting.")

    if text != text.strip():
        raise SettingsWriteError(
            f"{name}: leading or trailing spaces are stripped when the file is "
            f"read, so this would not come back as it went in.")

    if isinstance(value, list) and any("," in str(part) for part in value):
        raise SettingsWriteError(
            f"{name}: list entries are separated by commas, so an entry "
            f"containing one would come back as two.")

    # The general case, rather than a list of specific traps: write it, read it,
    # and see whether it is still the same value.
    try:
        back = coerce(name, text, namespace[name], types.get(name))
    except ValueError as err:
        raise SettingsWriteError(f"{name}: {err}") from None
    if back != value:
        raise SettingsWriteError(
            f"{name}: {value!r} would be read back as {back!r}.")

    return text, value


# The explanation appended above a setting that was not already in the file.
# A tuple, and consulted rather than retyped, so "is it already there?" and
# "what do we write?" cannot drift into two different answers - which is how
# a real install came to have it twice.
_ADDED_HEADER = (
    "# Added by DCCore because these settings were not already",
    "# in this file. Section headers are cosmetic; a setting",
    "# works wherever it appears.",
)


def _rewrite(existing, wanted):
    """The new text, editing lines in place and appending only what is missing.

    Returns (text, edited, added). `wanted` is {NAME: rendered text}.

    Two passes on purpose. The first only looks: for each setting it notes the
    line already setting it, if any, and separately the first commented-out
    default for it. The second edits exactly one line per setting.

    Doing both in one pass edits the commented default first and then the
    active line as well, which leaves the file setting the same key twice - and
    a file that sets a key twice is one parse() refuses in its entirety, taking
    all fifty other settings down with it.
    """
    lines = existing.split("\n")
    active_at, commented_at, seen_active = {}, {}, {}

    for index, line in enumerate(lines):
        found = _ASSIGNMENT_RE.match(line)
        if not found:
            continue
        key = found.group("key").upper()
        active = found.group("comment") is None

        if active:
            if key in seen_active:
                # The file was already broken before this save - parse() would
                # refuse it too. Say so plainly rather than editing one of the
                # two and leaving the other behind.
                raise SettingsWriteError(
                    f"{key} appears twice in the file already, on lines "
                    f"{seen_active[key] + 1} and {index + 1}. The daemon cannot "
                    f"read this file as it stands; remove one of the two first.")
            seen_active[key] = index

        if key not in wanted:
            continue

        # An ACTIVE line is anything configparser would read, "=" or ":", so a
        # line already setting this key is always found and never duplicated. A
        # COMMENTED line only counts as the sample's own commented-out default,
        # which always uses "=" - otherwise a prose comment like
        # "# MAX_DCC_SLOTS: how many at once" would be rewritten into a live
        # setting and the sentence lost.
        if active:
            active_at[key] = (index, found)
        elif found.group("sep") == "=" and key not in commented_at:
            commented_at[key] = (index, found)

    edited = {}
    for key in wanted:
        # The active line wins. Editing the commented default instead would
        # leave the operator's own uncommented value still in force, so the
        # dashboard would report a change that did not happen.
        where = active_at.get(key) or commented_at.get(key)
        if not where:
            continue
        index, found = where
        lines[index] = "%s%s%s= %s" % (
            found.group("indent"), found.group("key"),
            found.group("gap") or " ", wanted[key])
        edited[key] = index

    missing = [name for name in wanted if name not in edited]
    if missing:
        # Exactly one blank line between what the file already says and what
        # is being appended, however the file happened to end. The old form
        # added a blank line to whatever split("\n") had already left
        # behind, so a file ending with a newline - which is to say every file
        # this function has ever written - grew two blank lines per save.
        while lines and not lines[-1].strip():
            lines.pop()
        tail = [""]

        # ONCE. The header is prose explaining where these lines came from,
        # and a second copy explains nothing. A real install ended up with it
        # twice: the operator saved one setting from the dashboard and the
        # block was appended again underneath the one already there, because
        # this only ever asked whether a SETTING was missing and never
        # whether the EXPLANATION was.
        if not any(line.strip() == _ADDED_HEADER[0] for line in lines):
            tail.extend(_ADDED_HEADER)
        for name in missing:
            tail.append("%s = %s" % (name, wanted[name]))
        lines.extend(tail)

    text = "\n".join(lines)
    # A text file ends with a newline. The append path above could not
    # produce one - it extended the list past split("\n")'s trailing
    # "" - so every settings.conf that had ever had a setting added to it
    # ended mid-line. Harmless to parse() and wrong for everything else that
    # reads it, starting with the next append onto the end of that line.
    if text and not text.endswith("\n"):
        text += "\n"
    return text, sorted(edited), sorted(missing)

def _atomic_write(path, text):
    """Write `text` to `path` atomically, so a reader sees the whole old file
    or the whole new one and never a half-written one.

    The same temp-file-then-os.replace() db.py uses for every state file, and
    written out again here rather than imported: db.py imports config.py, and
    config.py imports this module, so reaching for it would close a cycle.
    """
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    handle, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".conf")
    try:
        # newline="": write exactly the bytes given, so the caller's
        # choice of line ending is what lands on disk.
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        platform_compat.replace_with_retry(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def shadowed_by_admin_config(names):
    """Which of `names` admin_config.py also sets.

    Not an error and not blocked - config.py applies admin_config.py first and
    this file second, so saving here is what takes effect. But an operator who
    keeps their real values in admin_config.py should be told that this is the
    point where that stops being true for a setting, rather than discovering it
    the next time they edit the Python file and nothing happens.
    """
    try:
        import admin_config
    except Exception:
        return []
    return sorted(name for name in names if hasattr(admin_config, name))


def save(namespace, changes, path=None, log=print):
    """Write `changes` into the settings file, editing it rather than
    replacing it.

    `namespace` is config.py's globals(), the same as apply_to() takes - it
    supplies both the list of real settings and each one's type. `changes` is
    {NAME: value} with values already in their Python types.

    Returns a report dict. Raises SettingsWriteError, before touching the disk,
    if any value could not be written and read back unchanged - so a caller
    that catches it knows the file is exactly as it was.

    The read-modify-write cycle below (read the existing file, compute the
    edited text, verify it, write it back) is held under _save_lock for its
    whole duration, not just the final write - two overlapping calls would
    otherwise each read the same starting file and each write back a version
    containing only their own change, silently losing whichever one lost the
    race even though each individual write is atomic on its own.
    """
    path = path or settings_path()
    if not changes:
        return {"path": path, "written": [], "added": [], "shadowed": []}

    types = declared_types(namespace)
    checked = {name: _check_writable(name, value, namespace, types)
               for name, value in changes.items()}
    wanted = {name: text for name, (text, _value) in checked.items()}
    expected = {name: value for name, (_text, value) in checked.items()}

    with _save_lock:
        existing, ending = "", "\n"
        if os.path.exists(path):
            try:
                with io.open(path, "rb") as handle:
                    raw = handle.read().decode("utf-8-sig")
            except (OSError, UnicodeDecodeError) as err:
                raise SettingsWriteError(
                    f"could not read the existing {os.path.basename(path)} to edit "
                    f"it: {err}") from None
            # Read as bytes on purpose: text mode turns CRLF into LF on the
            # way in, so writing back would quietly rewrite every line of a
            # file somebody last edited in Notepad. Work in "\n" throughout
            # and put the file's own ending back at the very end.
            if "\r\n" in raw:
                ending = "\r\n"
            existing = raw.replace("\r\n", "\n")

        text, edited, added = _rewrite(existing, wanted)

        # Nothing is written on the strength of having got the edit right. Read
        # the finished text back the way the daemon will, and check it says
        # what was asked for - a duplicated key raises here, and so does a
        # value that was written into a line that turned out not to mean what
        # it looked like.
        try:
            entries = parse(text)
        except SettingsError as err:
            raise SettingsWriteError(
                f"the edited file would not be readable ({err}), so it was not "
                f"written and {os.path.basename(path)} is unchanged.") from None

        for name, value in expected.items():
            if name not in entries:
                raise SettingsWriteError(
                    f"{name} did not survive the edit, so nothing was written.")
            try:
                back = coerce(name, entries[name], namespace[name], types.get(name))
            except ValueError as err:
                raise SettingsWriteError(f"{name} would not read back: {err}") from None
            if back != value:
                raise SettingsWriteError(
                    f"{name} would read back as {back!r} rather than {value!r}, "
                    f"so nothing was written.")

        _atomic_write(path, text if ending == "\n"
                      else text.replace("\n", ending))

    shadowed = shadowed_by_admin_config(changes)
    name = os.path.basename(path)
    log(f"[CONFIG] Wrote {len(changes)} setting(s) to {name}.")
    for setting in shadowed:
        log(f"[CONFIG] {name} now sets {setting}, which admin_config.py also "
            f"sets. This file is applied second, so this file wins from now on.")

    return {"path": path, "written": sorted(changes), "added": added,
            "shadowed": shadowed}
