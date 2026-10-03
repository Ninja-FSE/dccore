"""Stopping the bot without its window (#1065).

Until now the bot's window was the only way to stop it: close it, or Ctrl-C
in it. A bot run in the background (the rest of #1065), or by the autostart
task with its window minimised and forgotten, needs other ways - and all of
them end up in the same place Ctrl-C does, so there is one shutdown and not
four.

    request_stop()        says why, sends QUIT, and interrupts the main
                          thread: the KeyboardInterrupt run_forever() already
                          handles for Ctrl-C (flush the bot registry, exit 0).
    the stop file         data/dccore.stop. A small watcher looks for it every
                          two seconds; start-dccore stop (oserve.py --stop)
                          is what writes it.
    shutdown now          the admin console command (adminchat.py).
    Stop the bot          the dashboard's button (webserver.py).

A FILE, NOT A KILL. The stop command could have ended the process by its pid
- the instance lock already holds it - but Windows will not end a console
program without /F, which is a kill: no QUIT, no last flush. A file the bot
reads asks it to stop itself, the same way on every platform, and the lock
says when it really has: the command waits for the lock to come free.
"""

import _thread
import os
import sys
import threading
import time

import defaults as config
import platform_compat

STOP_FILE_NAME = "dccore.stop"
POLL_SECONDS = 2.0
WAIT_SECONDS = 60
QUIT_MESSAGE = "DCCore is stopping"


def _data_dir():
    """Where the lock lives: beside the queue file, as oserve.startup() has it."""
    return os.path.dirname(os.path.abspath(getattr(config, "DCC_QUEUE_FILE", os.path.join("data", "dcc_queue.txt"))))


def stop_file():
    return os.path.join(_data_dir(), STOP_FILE_NAME)


def _held_stop_file():
    """The stop file beside the lock THIS bot holds - not beside wherever
    DCC_QUEUE_FILE points now: a settings save reloads it live, and the
    watcher then looked in a folder the lock was never in (#1065 review)."""
    held = getattr(platform_compat, "_instance_lock", {}).get("path")
    return os.path.join(os.path.dirname(held), STOP_FILE_NAME) if held else stop_file()


def lock_file():
    return os.path.join(_data_dir(), "dccore.lock")


def request_stop(reason, interrupt=None):
    """Stop the bot the way Ctrl-C does. Safe from any thread."""
    print(f"[STOP] Stopping the bot: {reason}.")
    oserve = sys.modules.get("oserve")
    sock = getattr(oserve, "irc_connection", None) if oserve else None
    if sock is not None:
        try:
            sock.sendall(f"QUIT :{QUIT_MESSAGE}\r\n".encode("utf-8", errors="ignore"))
        except Exception:
            pass   # the connection is going anyway
    (interrupt or interrupt_main)()


def interrupt_main():
    """Ctrl-C, from any thread.

    A real SIGINT, not _thread.interrupt_main(): that only sets a flag the main
    thread looks at between two lines of Python, and a main thread asleep - the
    reconnect wait, up to five minutes - did not look until it woke, so
    `start-dccore stop` gave up after a minute (#1065 review). A real signal
    wakes the sleep at once. interrupt_main() is still there if one cannot be
    sent."""
    import signal
    try:
        if os.name == "nt":
            signal.raise_signal(signal.SIGINT)
        else:
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
    except (AttributeError, OSError, ValueError):
        _thread.interrupt_main()


def restore_interrupt():
    """At the program's start: Ctrl-C works however the bot was started.

    A bot started in the background by a script (`nohup python3 oserve.py &`)
    inherits SIGINT as ignored, and Python then installs no handler: every way
    to stop it here did nothing but send QUIT, and the bot reconnected ten
    seconds later (#1065 review). The program's own entry only - never a test
    runner's process."""
    import signal
    if signal.getsignal(signal.SIGINT) is signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.default_int_handler)


def request_stop_soon(reason, delay=1.0):
    """request_stop() a moment from now, on its own thread, so whoever asked
    gets their answer first - a console line, an HTTP response."""
    def later():
        time.sleep(delay)
        request_stop(reason)
    threading.Thread(target=later, name="stop-request", daemon=True).start()


# ------------------------------------------------------------ the stop file

def clear_stale_stop_file():
    """At startup: a stop file left from before is not a request to this bot."""
    try:
        os.remove(_held_stop_file())
    except OSError:
        pass


def check_stop_file(stop=request_stop):
    """One look. Returns True if a stop was asked for (and the file is gone)."""
    path = _held_stop_file()
    if not os.path.exists(path):
        return False
    try:
        os.remove(path)
    except OSError:
        pass
    stop("asked to by the stop command (start-dccore stop)")
    return True


def ensure_watcher():
    """Start the stop-file watcher once."""
    if runtime_watcher_alive():
        return
    import runtime
    watcher = threading.Thread(target=_watch, name="stop-file-watcher", daemon=True)
    runtime.stop_watcher_thread = watcher
    watcher.start()


def runtime_watcher_alive():
    import runtime
    watcher = getattr(runtime, "stop_watcher_thread", None)
    return watcher is not None and watcher.is_alive()


def _watch():
    # Watching on after a stop was asked for (#1065 review): if that one did
    # not take, the next `start-dccore stop` is still read. The file is gone
    # once read, so one request is acted on once.
    while True:
        time.sleep(POLL_SECONDS)
        try:
            check_stop_file()
        except Exception as err:
            print(f"[STOP] Could not look for the stop file: {err}")


def running_pid():
    """(running, pid): whether a bot holds this folder's lock, and its pid if
    it wrote one. Asked by taking the lock and letting it go at once."""
    try:
        platform_compat.take_instance_lock(lock_file())
    except platform_compat.AlreadyRunning as running:
        return True, running.pid
    platform_compat.release_instance_lock()
    return False, None


# ------------------------------------------------ the command (oserve.py --stop)

def stop_from_outside(wait=WAIT_SECONDS, sleep=time.sleep, log=print):
    """Ask the bot running from this folder to stop, and wait until it has.

    Returns 0 when it stopped or was not running, 1 when it did not stop in
    time. Taking the instance lock is how "is it running" is answered: the
    lock is free exactly when no bot holds it.
    """
    lock = lock_file()
    try:
        platform_compat.take_instance_lock(lock)
    except platform_compat.AlreadyRunning as running:
        pid = running.pid
    else:
        platform_compat.release_instance_lock()
        log("DCCore is not running from this folder - nothing to stop. (If DCC_QUEUE_FILE was "
            "moved while it runs, stop it from the dashboard or its window this once.)")
        return 0

    try:
        with open(stop_file(), "w", encoding="utf-8") as handle:
            handle.write("stop\n")
    except OSError as err:
        log(f"Could not ask DCCore to stop ({err}).")
        return 1
    who = f" (pid {pid})" if pid else ""
    log(f"Asked DCCore{who} to stop. Waiting for it to finish...")

    deadline = time.time() + wait
    while time.time() < deadline:
        sleep(1)
        try:
            platform_compat.take_instance_lock(lock)
        except platform_compat.AlreadyRunning:
            continue
        platform_compat.release_instance_lock()
        log("DCCore has stopped.")
        return 0

    force = (f"taskkill /F /PID {pid}" if os.name == "nt" else f"kill {pid}") if pid else None
    log(f"DCCore has not stopped after {wait} seconds. It may be in the middle of something "
        f"long; it will stop when it can."
        + (f" To end it now anyway: {force}" if force else ""))
    return 1
