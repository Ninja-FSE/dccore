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
CHOICES = {
    "LIST_FORMAT": ("txt", "zip", "rar"),
}


class SettingsError(Exception):
    """The file could not be parsed at all. Individual bad values do not
    raise - they are reported and skipped."""


def settings_path():
    return os.environ.get("DCCORE_SETTINGS_FILE") or DEFAULT_PATH


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
    """
    annotations = namespace.get("__annotations__") or {}
    return {name: kind for name, kind in annotations.items() if isinstance(kind, type)}


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
        return lowered

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

    return raw.strip("\n")


def apply_to(namespace, path=None, log=print):
    """Read the settings file and write recognised values into `namespace`.

    `namespace` is config.py's globals(). Returns a report dict; nothing here
    raises for ordinary problems, so a caller can ignore the return value and
    still get a correct daemon with the defaults intact.
    """
    path = path or settings_path()
    report = {"path": path, "applied": {}, "unknown": [], "bad": [], "read_error": None}

    if not os.path.exists(path):
        return report

    try:
        with io.open(path, encoding="utf-8") as handle:
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

    _log_summary(report, path, log)
    return report


def _log_summary(report, path, log):
    name = os.path.basename(path)
    if report["applied"]:
        log(f"[CONFIG] Applied {len(report['applied'])} setting(s) from {name}.")
    for key in report["unknown"]:
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

    text = render(value)

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
        tail = [] if existing.endswith("\n") or not existing else [""]
        tail.append("")
        tail.append("# Added by DCCore because these settings were not already")
        tail.append("# in this file. Section headers are cosmetic; a setting")
        tail.append("# works wherever it appears.")
        for name in missing:
            tail.append("%s = %s" % (name, wanted[name]))
        lines.extend(tail)

    return "\n".join(lines), sorted(edited), sorted(missing)

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
                    raw = handle.read().decode("utf-8")
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
