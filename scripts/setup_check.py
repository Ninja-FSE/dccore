"""Verify a DCCore setup WITHOUT connecting to IRC.

Run this before the first real start. It loads the same config the daemon
will and reports what it resolved - identity (NICKNAME/CHANNEL/ADMIN_NICK)
being blank is settings_file.REQUIRED's job, enforced hard by
oserve.startup() regardless of whether this check ever runs; what this adds
is everything REQUIRED does not cover: a music directory that is not there,
DCC port/slot misconfiguration, and the rest of the report below.

It never opens a socket to a server and never joins anything.

WHY ONE MODULE RATHER THAN ONE SCRIPT PER PLATFORM

scripts/linux/check-setup.py and scripts/windows/check-setup.py were the same
file twice - 151 identical lines out of ~220, checking the same ten settings,
differing only in a docstring, os.name, a rar hint and a few command names.

Two hand-maintained copies of the same knowledge about config.py is the shape
PRESERVE_RUNTIME already was here: a second list that had to be kept in step
with the first, drifted, and stopped matching reality without saying anything.

It had already begun. The Linux script's port messages explain that another
running instance is the usual reason a port is busy; the Windows one, written
first, never got that sentence. One improvement, one copy.

So the checks live here once, and each launcher's check-setup.py is a shim
that names its platform. Adding a check means editing one file, and issue
#100's mandatory settings will be one edit rather than two.

WHAT IS ACTUALLY PLATFORM-SPECIFIC

Only presentation and command names, gathered into Platform below. Every
actual check - what is read, what fails, what merely warns - is identical on
both, and always was.
"""

import os
import socket
import sys

# This module lives in scripts/, so the repo root is two levels up. Both
# shims are one level below that again, and neither needs to know: the paths
# are computed from THIS file, not from whichever script was invoked.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Platform:
    """The handful of strings that genuinely differ between the two hosts.

    Deliberately not a list of checks. If something here ever needs to be a
    behavioural difference rather than a wording one, that is a signal the
    seam is in the wrong place - the whole point is that both platforms are
    verified identically.
    """

    def __init__(self, display, os_name, wrong_os, rar_hint, python,
                 start_cmd, stop_where):
        self.display = display          # "Linux" / "Windows"
        self.os_name = os_name          # what os.name reads as
        self.wrong_os = wrong_os        # said when run on the other one
        self.rar_hint = rar_hint        # where rar was looked for
        self.python = python            # python3 / python
        self.start_cmd = start_cmd      # how to launch the daemon
        self.stop_where = stop_where    # "terminal" / "window"


LINUX = Platform(
    display="Linux",
    os_name="posix",
    wrong_os="this check is written for Linux; on Windows use "
             "scripts\\windows\\check-setup.py instead",
    rar_hint="no rar binary found on PATH",
    python="python3",
    start_cmd="./scripts/linux/start-dccore.sh",
    stop_where="terminal",
)

WINDOWS = Platform(
    display="Windows",
    os_name="nt",
    wrong_os="this check is written for Windows; on Linux use "
             "scripts/linux/check-setup.py instead",
    rar_hint="no rar/rar.exe found on PATH or in WinRAR's install directory",
    python="python",
    start_cmd="scripts\\windows\\start-dccore.bat",
    stop_where="window",
)


def main(platform):
    """Run every check and print the report. Returns the process exit code:
    1 if anything failed, 0 otherwise - warnings do not fail."""
    # Done here rather than at import. Importing a module must not move the
    # process's working directory out from under its caller - the scripts got
    # away with it because nothing ever imported them, and this one is meant
    # to be imported, including by its own tests.
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    os.chdir(REPO)

    problems = []
    warnings = []


    def fail(text):
        problems.append(text)
        print(f"  FAIL   {text}")


    def warn(text):
        warnings.append(text)
        print(f"  WARN   {text}")


    def ok(text):
        print(f"  ok     {text}")


    print()
    print("=" * 68)
    print("  DCCore setup check - nothing here connects to IRC")
    print("=" * 68)
    print()

    # --- interpreter and platform ------------------------------------------
    print("Environment")
    version = sys.version_info
    if version < (3, 10):
        fail(f"Python {version.major}.{version.minor} - the daemon needs 3.10 or newer")
    else:
        ok(f"Python {version.major}.{version.minor}.{version.micro}")

    if os.name != platform.os_name:
        warn(platform.wrong_os)
    else:
        ok(f"running on {platform.display}")

    # --- config -------------------------------------------------------------
    print()
    print("Configuration")

    # #162 finding #19: settings.conf is fully first-class - config.py applies
    # it SECOND (so it wins over admin_config.py on a shared key), the daemon
    # starts fine from it alone, and every setting an operator would otherwise
    # put in admin_config.py (including ADMIN_HOSTMASKS/ADMIN_PASSWORD_HASH -
    # see settings_file.is_overridable()) can live there instead. This used to
    # hard-fail whenever admin_config.py was absent, even when settings.conf
    # alone had already configured everything - contradicting this check's own
    # later "Applied N setting(s)" output on the very same run.
    admin_config_present = os.path.exists(os.path.join(REPO, "admin_config.py"))
    try:
        import settings_file
        settings_conf_present = os.path.exists(settings_file.settings_path())
    except Exception:
        settings_conf_present = os.path.exists(os.path.join(REPO, "settings.conf"))

    legacy_present = os.path.exists(os.path.join(REPO, "local_config.py"))

    if not admin_config_present and not settings_conf_present and legacy_present:
        # Not unconfigured - an upgrade that has not been started yet. #170
        # renamed local_config.py to admin_config.py, and because that file is
        # gitignored the pull could not rename the operator's own copy.
        #
        # This is NOT a failure, and saying so matters: the launcher refuses to
        # start when this check fails, and its message would send the operator
        # to admin_config.py.sample - which is exactly the condition that makes
        # defaults.py's migration skip, stranding their real settings for good.
        #
        # The rename has in fact already happened by the time anyone reads this
        # line: "import defaults" below runs it at import time, and the check
        # imports defaults a few lines further down. So this reports what was
        # done rather than asking for anything.
        ok("migrated local_config.py to admin_config.py (renamed in #170; the "
           "file is gitignored, so the upgrade could not rename it for you)")
    elif not admin_config_present and not settings_conf_present:
        fail("no admin_config.py and no settings.conf - copy admin_config.py.sample "
             "to admin_config.py, or settings.conf.sample to settings.conf, and fill "
             "one of them in, or the daemon will use the upstream defaults")
    elif not admin_config_present:
        ok("configured via settings.conf (no admin_config.py)")
    elif not settings_conf_present:
        ok("configured via admin_config.py")
    else:
        ok("configured via admin_config.py and settings.conf")

    try:
        import defaults as config
    except Exception as err:
        fail(f"config.py did not load: {err}")
        print()
        print("  Cannot continue without a config.")
        return 1

    import platform_compat  # noqa: E402

    ok(f"version {getattr(config, 'SCRIPT_VERSION', '?')}")
    ok(f"nickname {getattr(config, 'NICKNAME', '?')} "
       f"(alt {getattr(config, 'ALT_NICKNAME', '?')})")

    channels = str(getattr(config, "CHANNEL", ""))
    ok(f"channels {channels}")

    # The same check oserve.startup() makes, run here rather than only there.
    # This file's whole purpose is to be the friendlier, earlier warning, and
    # it used to defer this one to boot on the reasoning that boot catches it
    # "hard" - which it does, by exiting. So a fresh install was told "Ready to
    # start" by the pre-flight and then refused by the daemon seconds later,
    # with the pre-flight's verdict being the thing that was wrong.
    #
    # Called rather than reimplemented: one definition of what "configured"
    # means, so the two cannot drift apart again.
    try:
        import settings_file as _settings_file
        unconfigured = _settings_file.unconfigured_required(
            vars(config), getattr(config, "SHIPPED_DEFAULTS", {}))
    except Exception as err:
        unconfigured = []
        fail(f"could not check the required settings: {err}")
    for name in unconfigured:
        fail(f"{name} is still unconfigured (blank, or still the shipped "
             f"default) - the daemon refuses to start until it is set. Run "
             f"configure.py, or set it in settings.conf or admin_config.py.")

    # From #127. A slot count below 1 makes dcc.py's own gate
    # (len(active_transfers) < MAX_DCC_SLOTS) unsatisfiable, so the bot joins,
    # accepts requests into the queue, and dispatches none of them - with
    # nothing anywhere saying why.
    max_slots = int(getattr(config, "MAX_DCC_SLOTS", 3))
    if max_slots < 1:
        fail(f"MAX_DCC_SLOTS is {max_slots} - with no positive slot count the bot "
             f"can never dispatch a single transfer; every request just sits in "
             f"the queue forever")
    else:
        ok(f"max DCC slots {max_slots}")

    ok(f"debug channel {getattr(config, 'DEBUG_CHANNEL', '?')}")

    admin_nick_raw = str(getattr(config, "ADMIN_NICK", ""))
    ok(f"admin nick {admin_nick_raw}")

    # --- paths --------------------------------------------------------------
    print()
    print("Paths")

    # A WARN, not a FAIL, when unset: FILE_DIRECTORY is deliberately not in
    # settings_file.REQUIRED (see its own comment) - the daemon boots fine
    # without it chosen yet, specifically so the web dashboard's own
    # Settings page can be where it gets set. A value that IS set but wrong
    # stays a hard FAIL - that is a real misconfiguration, not an unmade
    # choice.
    music = getattr(config, "FILE_DIRECTORY", "")
    if not music:
        warn("FILE_DIRECTORY is not set yet - the daemon will start, but cannot "
             "search or serve anything until it is set from the web dashboard's "
             "Settings page, settings.conf, or admin_config.py.")
    elif not os.path.isdir(music):
        fail(f"FILE_DIRECTORY does not exist: {music}  "
             f"(the daemon exits at startup if this is set but missing)")
    else:
        count = 0
        for _root, _dirs, files in os.walk(music):
            count += sum(1 for f in files if f.lower().endswith((".mp3", ".flac")))
            if count > 5000:
                break
        ok(f"music directory {music}")
        ok(f"{'over 5000' if count > 5000 else count} audio file(s) visible - "
           f"the first scan walks all of them")

    for label, path in (("lists", getattr(config, "LOCAL_LIST_DIR", "")),
                        ("temp archives", getattr(config, "TMP_ZIP_DIR", ""))):
        resolved = os.path.abspath(path) if path else "(unset)"
        if path and not os.path.isabs(path):
            ok(f"{label} -> {resolved}  (relative: correct only when started "
               f"from the repo folder)")
        else:
            ok(f"{label} -> {resolved}")

    # --- external tools ------------------------------------------------------
    print()
    print("Tools")
    rar = platform_compat.rar_command(getattr(config, "RAR_BINARY", None))
    if rar:
        ok(f"rar {rar}")
    else:
        warn(f"{platform.rar_hint} - whole-album (!rar) packing will fail, "
             f"single files are unaffected")

    # --- DCC ports -----------------------------------------------------------
    print()
    print("DCC ports")
    start = int(getattr(config, "DCC_PORT_START", 55000))
    end = int(getattr(config, "DCC_PORT_END", 55010))
    # From #127. range(start, end + 1) is empty when start > end, so the bind
    # loop below would find 0 free ports and report "something else is using
    # them" - sending the operator after a phantom port conflict instead of the
    # two swapped values that are the actual, one-line problem. Checked before
    # the loop, and the loop skipped entirely, rather than left to produce a
    # misleading answer.
    if start > end:
        fail(f"DCC_PORT_START ({start}) is greater than DCC_PORT_END ({end}) - "
             f"the range is empty, so no DCC transfer can ever open a listening "
             f"port; check for the two values being swapped in admin_config.py")
    else:
        free = 0
        for port in range(start, end + 1):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                platform_compat.prepare_listener(probe)
                probe.bind(("0.0.0.0", port))
                free += 1
            except OSError:
                pass
            finally:
                probe.close()

        if free == 0:
            fail(f"no port in {start}-{end} could be bound - something else is using "
                 f"them (another instance of this daemon running at the same time is "
                 f"the common cause)")
        elif free < (end - start + 1):
            warn(f"{free} of {end - start + 1} ports free in {start}-{end} - if "
                 f"another instance of this daemon is running, it is normal for it "
                 f"to be holding some of these")
        else:
            ok(f"all {free} ports free in {start}-{end}")
        print("         (these must also be forwarded to this machine for "
              "anyone to download from you)")

    # --- admin console -------------------------------------------------------
    print()
    print("Admin console")
    masks = getattr(config, "ADMIN_HOSTMASKS", []) or []
    has_hash = bool(getattr(config, "ADMIN_PASSWORD_HASH", ""))
    if not masks:
        ok("disabled (ADMIN_HOSTMASKS is empty) - this is fine")
    elif not has_hash:
        # A warning, not a failure. With no hash the console refuses every
        # connection, so it fails CLOSED - nothing unsafe happens, the feature is
        # just off. Blocking the whole daemon over an optional feature that is
        # safely inert only teaches people to skip the check.
        warn(f"ADMIN_HOSTMASKS is set but ADMIN_PASSWORD_HASH is empty - the console "
             f"will refuse every connection until you run: {platform.python} adminchat.py")
    else:
        ok(f"enabled for {len(masks)} host pattern(s)")

    # --- verdict --------------------------------------------------------------
    print()
    print("=" * 68)
    if problems:
        print(f"  {len(problems)} problem(s) - fix these before starting:")
        for text in problems:
            print(f"    - {text}")
        print("=" * 68)
        return 1

    if warnings:
        print(f"  Ready to start, with {len(warnings)} warning(s).")
    else:
        print("  Ready to start.")
    print()
    print(f"  Start it with:  {platform.start_cmd}")
    print(f"  Stop it with:   Ctrl-C in that {platform.stop_where}")
    print("=" * 68)
    print()
    return 0
