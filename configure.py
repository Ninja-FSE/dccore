# configure.py - guided first-run configuration for a new DCCore install.
"""Answers settings_file.REQUIRED (NICKNAME, CHANNEL, ADMIN_NICK) plus
SERVER, the admin console password, the web dashboard, and (optionally) the
music directory, then writes them where they actually belong, so a fresh
checkout can go from "just cloned" to "the daemon will actually boot and do
something useful" in one guided pass instead of hand-editing two files by
comparing them against admin_config.py.sample/settings.conf.sample.

#170's RFC (issue #170's discussion) originally asked for exactly six
fields, FILE_DIRECTORY included as a REQUIRED one. Two things changed after
real end-to-end test runs against a real install:

  * FILE_DIRECTORY moved OUT of settings_file.REQUIRED entirely (see its own
    comment there) - requiring it here meant the daemon could not boot at
    all without a music directory chosen up front, which is the one thing
    the dashboard's own Settings page is a genuinely easier place to set
    (browse-and-confirm rather than typing a path blind at a prompt) - but
    the dashboard needs the daemon RUNNING to reach it. Asked here anyway,
    but optional: leaving it blank is a supported "set it later" choice, not
    an error.
  * Two questions were added to close a "boots but is not actually usable
    yet" gap between "the daemon starts" and "somebody can actually use
    it": whether to turn the web dashboard on at all (it ships off, on
    loopback only, and a first-run script answering "can I log in" for the
    DCC console but leaving the dashboard unreachable by default is an
    inconsistent amount of guidance), and whether to run the initial
    library scan now (skipping it silently leaves a daemon that joins its
    channels and answers nothing until somebody thinks to run !update or
    update_list.py by hand).

Everything else this daemon can be tuned with - the ~50 other
settings.conf.sample entries, the rest of the dashboard's own Settings page
- is still deliberately NOT asked here.

WHERE EACH ANSWER GOES

    NICKNAME, SERVER, CHANNEL, ADMIN_NICK, WEBUI_ENABLED,
    WEBUI_HOST, FILE_DIRECTORY (if given)                    -> settings.conf
    ADMIN_PASSWORD_HASH                                      -> admin_config.py

The first five are ordinary operational settings - settings_file.py already
treats them as primitives any operator can change from the CLI or the
dashboard's Settings page, so settings.conf (the mechanism BOTH of those
already write to) is where they belong, via settings_file.save() - the exact
same read-modify-write-and-verify path the dashboard's own settings route
uses, not a hand-rolled file write. ADMIN_PASSWORD_HASH is different: a
credential, never meant to be edited from a web form running under the
password it protects, so it goes to admin_config.py (Python, gitignored)
instead - matching what running `python3 adminchat.py` directly has always
told an operator to do with the hash it prints.

WHY THE PASSWORD STEP REUSES adminchat.py RATHER THAN REPROMPTING

adminchat.make_password_hash() and adminchat._read_password() are imported
and called directly, in-process - not by shelling out to
`python3 adminchat.py` as a subprocess and parsing its printed output, which
would mean two ways to get a password into this process (once through this
script's own prompt, once through whatever adminchat.py's subprocess did
with its inherited stdin) and a fragile text-scrape of a line meant for a
human to read. Importing the same function this project already uses everywhere
else that needs this hash is the "one definition, not two" pattern this
codebase leans on repeatedly - PRESERVE_RUNTIME, the shared PRIVMSG/NOTICE
parsers, the rar-archive-name sanitiser, settings_file.REQUIRED itself.
_read_password() already handles the no-real-terminal case (falls back to
plain, announced stdin) on its own, so this script does not need to.

RE-RUNNING THIS SCRIPT IS SAFE

Every prompt shows the CURRENTLY CONFIGURED value (from whatever
admin_config.py/settings.conf already resolve to) as its default - press
Enter to keep it. An operator who already has a working install and just
wants to change one thing can run this again rather than hand-editing
settings.conf, and nothing here can silently revert an already-correct
answer to a blank one.
"""

import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import settings_file  # noqa: E402


def _ask(prompt, default=None):
    """One prompt, with `default` shown and used on a bare Enter.

    Every field here either has a real, always-non-blank default (SERVER,
    via _current()'s own fallback) or is in settings_file.REQUIRED and
    refuses to boot on blank - so a bare Enter with nothing to fall back to
    is re-prompted rather than accepted, which is what actually gets the
    operator to a bootable daemon, instead of a technically-finished setup
    run that still fails at oserve.startup() with no further guidance.
    """
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw:
            if default:
                return default
            print("  This can't be blank - it's required before the daemon will start.")
            continue
        return raw


def _current(name, fallback=""):
    """The currently resolved value for `name`, or `fallback` if it is still
    unset (settings_file.REQUIRED ships these blank - see config.py's own
    comment) - what an already-partially-configured install already has,
    shown back as this prompt's own default so re-running never silently
    reverts a real answer to a placeholder."""
    value = getattr(config, name, None)
    return value if value else fallback


def collect_answers():
    """Returns (changes, password_hash). `changes` is already the exact
    {NAME: value} dict write_settings_conf() writes as-is - built here,
    incrementally, rather than filtered afterwards, so the ONE place that
    knows whether an answer was actually given (a blank SERVER; the web
    dashboard left off) is the one place that decides whether it is written
    at all.
    """
    print("=" * 68)
    print("  DCCore first-run setup")
    print("=" * 68)
    print()
    print("A handful of questions, then the daemon should be able to start")
    print("and actually serve something. Anything else - the ~50 other")
    print("settings, admin console hostmasks, the rest of the dashboard - is")
    print("configured afterwards, from settings.conf, the dashboard's own")
    print("Settings page, or admin_config.py directly.")
    print()

    changes = {}

    nickname = _ask("Nickname", default=_current("NICKNAME"))
    changes["NICKNAME"] = nickname

    server = _ask("IRC server", default=_current("SERVER", "irc.undernet.org"))
    changes["SERVER"] = server

    channel = _ask("Channel(s), comma-separated", default=_current("CHANNEL"))
    changes["CHANNEL"] = channel

    admin_nick = _ask("Admin nick (who may run !ban/!rehash/!update/!clearqueue)",
                      default=_current("ADMIN_NICK"))
    changes["ADMIN_NICK"] = admin_nick

    print()
    print("Admin console password (for the DCC CHAT console - see")
    print("docs/ADMIN-CONSOLE.md). Same prompt as running adminchat.py by itself.")
    while True:
        first = adminchat._read_password("Password: ")
        second = adminchat._read_password("Again: ")
        if not first:
            print("  Empty password refused.")
            continue
        if first != second:
            print("  Passwords did not match - try again.")
            continue
        break
    password_hash = adminchat.make_password_hash(first)

    print()
    print("Music directory (optional) - not required to start the daemon.")
    print("Easier to browse-and-confirm from the web dashboard's Settings")
    print("page once it is running, if you would rather do it there.")
    current_file_directory = _current("FILE_DIRECTORY")
    suffix = f" [{current_file_directory}]" if current_file_directory else ""
    file_directory = input(f"Music directory (full path){suffix}: ").strip() or current_file_directory
    while file_directory and not os.path.isdir(file_directory):
        print(f"  {file_directory!r} does not exist.")
        create = input("  Create it now? [y/N]: ").strip().lower()
        if create in ("y", "yes"):
            try:
                os.makedirs(file_directory, exist_ok=True)
            except OSError as err:
                print(f"  Could not create it: {err}")
            else:
                break
        else:
            file_directory = input("  Music directory (full path, or leave "
                                   "blank to set it later): ").strip()
    if file_directory:
        changes["FILE_DIRECTORY"] = file_directory
    else:
        print("  Skipped - set it later from the dashboard, settings.conf, "
              "or admin_config.py.")

    print()
    print("Web dashboard (optional) - search / queue / file lists, over a")
    print("small Flask app in a browser. Off by default; needs Flask")
    print("installed (`pip install flask`), but never stops the daemon from")
    print("starting if it is not.")
    enable_webui = input("Enable it? [y/N]: ").strip().lower() in ("y", "yes")
    changes["WEBUI_ENABLED"] = enable_webui
    if enable_webui:
        lan = input("  Reachable from other devices on your LAN (phone, "
                    "laptop), not just this machine? [y/N]: ").strip().lower() in ("y", "yes")
        changes["WEBUI_HOST"] = "0.0.0.0" if lan else "127.0.0.1"
        if lan:
            print("  WARNING: there is no TLS here. A login is required for every")
            print("  route, but do not put this host on a network you do not trust,")
            print("  and never port-forward this port to the internet.")

    return changes, password_hash


def write_settings_conf(changes, path=None):
    """A thin pass-through to settings_file.save() - the exact same
    read-modify-write-and-verify path the dashboard's Settings page and CLI
    edits already use, not a hand-rolled write. `changes` is already exactly
    what should be written - collect_answers() decides what belongs in it
    (a blank SERVER, or the web dashboard left off, are both deliberately
    absent rather than written as an explicit "no" - config.py's own real
    defaults already say that). `path` overrides settings_file's own
    default location - tests use it, real runs never pass it."""
    # save() already logs its own "[CONFIG] Wrote N setting(s)..." line
    # (log=print by default) - nothing more to print here.
    return settings_file.save(vars(config), changes, path=path)


def build_admin_config_text(existing_text, password_hash):
    """The pure edit: `existing_text` (whatever admin_config.py, or failing
    that admin_config.py.sample, or failing that a bare fresh comment,
    already reads as - see write_admin_config_password()) with
    ADMIN_PASSWORD_HASH set to `password_hash`, replacing an existing
    assignment in place or appending a new one. Pulled out as a pure
    function, no file I/O, so the text transformation itself is directly
    testable - matching this codebase's own settled pattern for exactly
    this reason (rehash_nick_change_line(), subprocess_failure_message(),
    and others all exist for the identical reason).

    Deliberately narrow: only ADMIN_PASSWORD_HASH is ever touched. A hand-
    edited ADMIN_HOSTMASKS (or anything else already in the file) survives
    untouched either way.
    """
    new_line = f'ADMIN_PASSWORD_HASH = "{password_hash}"'
    pattern = re.compile(r'^ADMIN_PASSWORD_HASH\s*=.*$', re.MULTILINE)
    if pattern.search(existing_text):
        return pattern.sub(new_line, existing_text, count=1)
    text = existing_text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + new_line + "\n"


def write_admin_config_password(password_hash, path=None, sample_path=None):
    """Only ADMIN_PASSWORD_HASH - never overwrites anything else a hand-
    edited admin_config.py already has (ADMIN_HOSTMASKS, most notably).
    Replaces an existing ADMIN_PASSWORD_HASH line in place if this is a
    re-run; appends a new one otherwise, creating the file from
    admin_config.py.sample's own template if it does not exist yet at all.
    `path`/`sample_path` override the real repo locations - tests use them,
    real runs never pass them.
    """
    path = path or os.path.join(REPO_ROOT, "admin_config.py")
    sample_path = sample_path or os.path.join(REPO_ROOT, "admin_config.py.sample")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            existing_text = handle.read()
    elif os.path.exists(sample_path):
        with open(sample_path, "r", encoding="utf-8") as handle:
            existing_text = handle.read()
    else:
        existing_text = "# admin_config.py - created by configure.py\n"

    text = build_admin_config_text(existing_text, password_hash)

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"[SETUP] Wrote ADMIN_PASSWORD_HASH to {os.path.basename(path)}.")


def offer_to_generate_master_list(file_directory_set):
    """update_list.py, run as its own subprocess - exactly the way !update
    already does it (commands.py's handle_list_update_request). A fresh
    process picks up the settings.conf/admin_config.py this script just
    wrote the same way the real daemon would at its next start, rather than
    re-importing update_list.py in-process against whatever this script's
    own already-loaded `defaults` module happens to hold.

    Without this step, a freshly set-up daemon joins its channels and
    answers every request with silence: it has no master list until
    somebody runs !update from IRC or update_list.py by hand, and nothing
    before this existed to say so - found the hard way, running this
    script against a real library for the first time.

    `file_directory_set` is whether collect_answers() actually got one -
    FILE_DIRECTORY is optional now (see settings_file.REQUIRED's own
    comment), and there is nothing to scan yet if it was skipped, so this
    is skipped too rather than prompting for an action that can only fail.
    """
    print()
    if not file_directory_set:
        print("[SETUP] No music directory was set, so there is nothing to scan yet.")
        print("        Once one is set (dashboard, settings.conf, or admin_config.py),")
        print("        run 'python3 update_list.py', or !update from IRC.")
        return
    print("Generate the master file list now? This walks the whole music")
    print("directory - it can take a while for a large library - and is")
    print("required before anyone can download anything from this bot.")
    skip = input("Generate it now? [Y/n]: ").strip().lower() in ("n", "no")
    if skip:
        print("[SETUP] Skipped. Run 'python3 update_list.py', or !update from")
        print("        IRC once the daemon is running, before it is usable.")
        return
    print("[SETUP] Running update_list.py - this may take a while...")
    result = subprocess.run([sys.executable, os.path.join(REPO_ROOT, "update_list.py")])
    if result.returncode != 0:
        print("[SETUP] List generation failed - see the output above. Re-run")
        print("        'python3 update_list.py', or !update from IRC, once")
        print("        the problem is fixed.")


def main():
    changes, password_hash = collect_answers()
    write_settings_conf(changes)
    write_admin_config_password(password_hash)
    offer_to_generate_master_list("FILE_DIRECTORY" in changes)

    print()
    print("=" * 68)
    print("  Setup complete.")
    print("=" * 68)
    print()
    print("Verify it before the first real start:")
    print("    ./scripts/linux/start-dccore.sh check      (Linux)")
    print("    scripts\\windows\\start-dccore.bat check     (Windows)")
    print()
    print("Then start the daemon the same way, without \"check\".")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled - nothing was written past what already "
              "completed above.")
        sys.exit(1)
