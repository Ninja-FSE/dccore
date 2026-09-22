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

Two hand-maintained copies of the same knowledge about defaults.py is the shape
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
                 start_cmd, stop_where, pip_hint, firewall_hint):
        self.display = display          # "Linux" / "Windows"
        self.os_name = os_name          # what os.name reads as
        self.wrong_os = wrong_os        # said when run on the other one
        self.rar_hint = rar_hint        # where rar was looked for
        self.python = python            # python3 / python
        self.start_cmd = start_cmd      # how to launch the daemon
        self.stop_where = stop_where    # "terminal" / "window"
        # How to install an optional dependency INTO THE INTERPRETER THE
        # LAUNCHER USES. A bare `pip` follows whatever `python` resolves to,
        # which on a machine with more than one is not necessarily the one
        # that will run the daemon - see the Flask check further down.
        self.pip_hint = pip_hint
        # What stands between the bound ports and the outside on THIS OS,
        # and what to do about it. Printed after the port check, with the
        # range filled in (#547, Proposal 6). Wording only: both hosts have
        # a host firewall, they just ask about it differently.
        self.firewall_hint = firewall_hint


LINUX = Platform(
    display="Linux",
    os_name="posix",
    wrong_os="this check is written for Linux; on Windows use "
             "scripts\\windows\\check-setup.py instead",
    rar_hint="no rar binary found on PATH",
    python="python3",
    start_cmd="./scripts/linux/start-dccore.sh",
    stop_where="terminal",
    pip_hint="python3 -m pip install -r requirements-web.txt",
    firewall_hint="if this machine runs a firewall, allow them: "
                  "sudo ufw allow {start}:{end}/tcp (Ubuntu), or "
                  "sudo firewall-cmd --permanent --add-port={start}-{end}/tcp "
                  "&& sudo firewall-cmd --reload (Fedora)",
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
    # `py -3 -m pip`, not a bare `pip`. start-dccore.bat prefers `py -3` and
    # only falls back to `python`, so on a machine with both this is the one
    # that puts the package where the daemon will actually look for it.
    pip_hint="py -3 -m pip install -r requirements-web.txt",
    firewall_hint="Windows Defender Firewall asks the first time the bot "
                  "listens on {start}-{end}; if that was cancelled, every "
                  "send times out - scripts\\windows\\allow-firewall.bat "
                  "adds the rule (asks for an administrator's yes)",
)


def ports_line(config):
    """The ports the daemon listens on, one line, for the firewall and
    autostart helpers beside this file (#547, Proposal 6):

        DCC_PORT_START DCC_PORT_END WEBUI_PORT 1|0(WEBUI_ENABLED)

    Here rather than in scripts/ports.py itself because this module is the
    one place that knows the port settings - a test counts the copies."""
    return "%d %d %d %d" % (
        int(getattr(config, "DCC_PORT_START", 55000)),
        int(getattr(config, "DCC_PORT_END", 55010)),
        int(getattr(config, "WEBUI_PORT", 8420)),
        1 if bool(getattr(config, "WEBUI_ENABLED", False)) else 0,
    )


def library_report(config, ok, warn, fail, detail):
    """The Paths section's library check, resolved the way the daemon resolves it.

    Asked of library.folders(), NOT of FILE_DIRECTORY. That setting is only the
    fallback for an install with no folder list, and this check treated it as
    the only truth - so an operator who had configured folders on the
    dashboard and left FILE_DIRECTORY blank was told at every start that the
    daemon "cannot search or serve anything until it is set", which was simply
    untrue. oserve.py's own startup check received exactly this correction
    (see its comment above `configured = library.folders()`); the setup check
    had kept the old rule, and it is what the operator reads first.

    The three outcomes are the daemon's own, so this cannot say "ready" for a
    library the daemon will refuse, or refuse one it will serve:

      no folders at all  -> WARN.  Not chosen yet, not misconfigured. The
                            daemon boots so the dashboard can be where it
                            gets set.
      every folder gone  -> FAIL.  The daemon exits at startup on this, and
                            so does update_list's own entry point.
      some folders gone  -> WARN.  A scan-time condition the build already
                            skips with a warning. Not worth refusing for,
                            and the daemon does not.

    An install with only FILE_DIRECTORY set reads exactly as it did before:
    that path is unchanged, wording included. The per-folder listing is new
    and only appears when a folder file is in use.

    `detail` prints an indented sub-line under the last ok/warn/fail; it is
    a reporter like the other three rather than a print() here, so this
    function - which sits above main()'s console-encoding guard in the file -
    holds no print of its own for that guard's ordering test to trip on.

    Pulled out of main() so it can be exercised with a fake config and
    recording reporters, instead of only through a child process against
    whatever config the developer's own checkout happens to hold.
    """
    import library
    import update_list

    configured = library.folders()
    if not configured:
        warn("no music folders configured yet - the daemon will start, but cannot "
             "search or serve anything until a folder is added from the web "
             "dashboard's Library page, or FILE_DIRECTORY is set in settings.conf "
             "or admin_config.py.")
        return

    def count_listed(folders):
        # The SAME predicate the list build uses, not a second copy of it. A
        # count here that disagreed with what update_list.py indexes would
        # report a healthy library and then publish a list that does not
        # match it - which is the shape of the defect that made this a
        # setting at all.
        total = 0
        for folder in folders:
            for _root, _dirs, files in os.walk(folder.path):
                total += sum(1 for f in files if update_list.is_listed_file(f))
                if total > 5000:
                    return total
        return total

    def report_count(folders):
        count = count_listed(folders)
        ok(f"{'over 5000' if count > 5000 else count} file(s) would be "
           f"listed - the first scan walks all of them")

    # No folder file: the single FILE_DIRECTORY, exactly as before.
    if library.load_folders() is None:
        music = configured[0].path
        if not os.path.isdir(music):
            fail(f"FILE_DIRECTORY does not exist: {music}  "
                 f"(the daemon exits at startup if this is set but missing)")
            return
        ok(f"music directory {music}")
        report_count(configured)
        return

    # A folder file: every folder, by name, with its reachability.
    present = [f for f in configured if os.path.isdir(f.path)]
    missing = [f for f in configured if f not in present]

    ok(f"library: {len(configured)} folder(s) from "
       f"{os.path.basename(library.folders_file())}, {len(present)} reachable")
    for folder in configured:
        state = "ok     " if folder in present else "MISSING"
        detail(f"{state}  {folder.name} -> {folder.path}")

    if not present:
        fail("none of the configured music folders exist - the daemon exits at "
             "startup on this, and so does the list build")
        return
    if missing:
        warn(f"{len(missing)} of {len(configured)} folder(s) cannot be reached right "
             f"now - the list build skips a missing folder with a warning, so "
             f"the daemon will start, but its list will be short until the "
             f"drive is back")
    report_count(present)


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

    # #428: BEFORE the first print below, not after `import defaults`
    # succeeds a hundred-odd lines down. start-dccore.bat runs this check
    # with stdout redirected (`>nul 2>&1`), which makes Python fall back to
    # the machine's ANSI code page instead of the real console encoding
    # (PEP 528 only applies to an actual console). A configured path holding
    # a character that code page cannot spell then raised UnicodeEncodeError
    # on one of the many prints between here and the old import site - the
    # launcher printed "Setup check failed - not starting", re-ran the same
    # check on the real console where PEP 528 makes it succeed, and reported
    # "Ready to start." The refusal and the diagnosis contradicted each
    # other, and the daemon never started, for a reason nothing in the
    # report explained. Same guard oserve.py and update_list.py install at
    # their own top, before their own first print.
    import platform_compat
    platform_compat.install_console_encoding_guard()

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

    def detail(text):
        print(f"           {text}")


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

    # #162 finding #19: settings.conf is fully first-class - defaults.py applies
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
        # Not "copy the sample" (#685, audit L21): that is the manual step
        # the launchers replaced (#547), and a novice who followed it
        # created admin_config.py by hand - which is the launcher's
        # first-run gate - so the questions and the browser page were never
        # offered, and the copied sample turned the dashboard and the debug
        # channel on for them.
        fail("no admin_config.py and no settings.conf - nothing is configured yet. "
             f"Run {platform.start_cmd} (it asks the questions, or opens the setup page "
             f"in your browser), or {platform.python} configure.py")
    elif not admin_config_present:
        ok("configured via settings.conf (no admin_config.py)")
    elif not settings_conf_present:
        ok("configured via admin_config.py")
    else:
        ok("configured via admin_config.py and settings.conf")

    try:
        import defaults as config
    except Exception as err:
        # defaults.py, the module's name since the rename the guides
        # describe (#699): the message said "config.py", a file that does
        # not exist, and sent an operator looking for it.
        fail(f"defaults.py did not load (it reads admin_config.py and settings.conf): {err}")
        print()
        print("  Cannot continue without a config.")
        return 1

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

    # Asked of the LIBRARY, not of FILE_DIRECTORY - the same correction
    # oserve.py's startup check already received. See library_report().
    library_report(config, ok, warn, fail, detail)

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

    # Flask, checked with THIS interpreter on purpose. The launcher runs both
    # the daemon and this check through the same `%PY%` / `$PY`, so importing
    # it here answers the only question that matters: will the dashboard come
    # up when the bot starts.
    #
    # Found in beta. A machine with both a `py` launcher and a `python` on
    # PATH can have two interpreters - and `pip install -r requirements-web
    # .txt`, which is what the docs say, follows `python` while the launcher
    # prefers `py -3`. Flask went into one and the daemon started under the
    # other, so the dashboard was silently absent and the only clue was a log
    # line AFTER the bot had already connected. The check said "Ready to
    # start" and meant it - it just was not answering this question.
    if getattr(config, "WEBUI_ENABLED", False):
        try:
            import flask  # noqa: F401
            ok("Flask available - the web dashboard will start")
        except ImportError:
            warn(f"WEBUI_ENABLED is on but Flask is not installed FOR THIS "
                 f"INTERPRETER ({sys.executable}) - the daemon will start and "
                 f"the dashboard will not. Install it with the same one: "
                 f"{platform.pip_hint}")
    else:
        ok("web dashboard disabled (WEBUI_ENABLED) - Flask not needed")

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
        print("         " + platform.firewall_hint.format(start=start, end=end))

    # --- admin console -------------------------------------------------------
    print()
    print("Admin console")
    # ASK THE DAEMON WHAT IT WILL ACTUALLY ACCEPT (#447), rather than counting
    # the raw list. ADMIN_HOSTMASKS = [""] is a truthy list of one, so
    # counting it reported "enabled for 1 host pattern(s)" while
    # adminchat.admin_host_patterns() returned [] and the console was in fact
    # off - and [""] is exactly what a fresh install can end up with, because
    # configure.py's password prompt is mandatory while the hostmask is not.
    #
    # A pre-flight check that reports a feature as enabled when it is disabled
    # is worse than one that says nothing: the operator stops looking.
    masks = getattr(config, "ADMIN_HOSTMASKS", []) or []
    try:
        import adminchat
        patterns = adminchat.admin_host_patterns()
    except Exception:
        # The check must never be what breaks the check. Fall back to the raw
        # list, stripped, which is the same question asked less precisely.
        patterns = [m for m in masks if str(m or "").strip()]
    has_hash = bool(getattr(config, "ADMIN_PASSWORD_HASH", ""))
    if not patterns:
        if masks:
            ok("disabled (ADMIN_HOSTMASKS has no usable pattern) - this is fine")
        else:
            ok("disabled (ADMIN_HOSTMASKS is empty) - this is fine")
    elif not has_hash:
        # A warning, not a failure. With no hash the console refuses every
        # connection, so it fails CLOSED - nothing unsafe happens, the feature is
        # just off. Blocking the whole daemon over an optional feature that is
        # safely inert only teaches people to skip the check.
        warn(f"ADMIN_HOSTMASKS is set but ADMIN_PASSWORD_HASH is empty - the console "
             f"will refuse every connection until you run: {platform.python} adminchat.py")
    else:
        ok(f"enabled for {len(patterns)} host pattern(s)")
        # Accepted, but far wider than one operator (#669): a wildcard where
        # the account name goes, or a bare top-level domain.
        try:
            import adminchat as _adminchat_breadth
            broad = _adminchat_breadth.broad_host_patterns()
        except Exception:
            broad = []
        for pattern, why in broad:
            warn(f"ADMIN_HOSTMASKS entry {pattern!r} is very broad - {why}. Anyone "
                 f"matching it reaches the console's password prompt; write your own "
                 f"services host in full, e.g. 'operator.users.undernet.org'")

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
