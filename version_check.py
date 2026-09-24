"""Is there a newer DCCore? (#572)

Once a day the bot asks the public repository's latest release -
GET https://api.github.com/repos/<owner>/<name>/releases/latest, the repository
taken from PROJECT_URL - and compares its tag with SCRIPT_VERSION. One
unauthenticated request a day, far below GitHub's limit of 60 an hour. The
request tells GitHub this machine's address and that it runs DCCore; nothing
about the bot, its library or its channels is sent.

ON BY DEFAULT, VISIBLY. An update notice that is off by default reaches the
operators who never open Settings - exactly the ones who miss updates, security
fixes included. So it is on, pre-ticked on the setup page for a new install and
announced at startup for one that upgrades into it (CHECK_FOR_UPDATES turns it
off). This replaces #547's "no phone-home without a tickbox": the tickbox is
there, ticked, and said out loud.

NEVER SILENT WHEN IT FAILS. A machine that cannot reach GitHub is told why: one
console/debug line per failed attempt - at most daily, so a box that is never
allowed out does not fill up with them - and a persistent "could not check"
state on the dashboard and in the console's `status` until a check succeeds. A
manual check (the dashboard's Check now, the console's `checkversion`) always
reports what happened, and works with the daily check turned off: clicking it
is the consent.

Only a full release counts: GitHub's /releases/latest never returns a draft or
a pre-release, and a tag that is not plain vMAJOR.MINOR.PATCH is not compared
at all. A bot running a release candidate is behind the release of the same
number. The state lives in runtime.py, so a rehash neither loses it nor starts
a second worker.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

import defaults as config
import runtime

CHECK_INTERVAL_SECONDS = 24 * 3600
FIRST_CHECK_DELAY_SECONDS = 300      # let the bot connect before the first one
WORKER_TICK_SECONDS = 600
MANUAL_COOLDOWN_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10


class CheckFailed(Exception):
    """Why a check could not be completed, worded for the operator."""


def repository(project_url=None):
    """'owner/name' from a github.com URL (PROJECT_URL by default), or None."""
    url = project_url if project_url is not None else getattr(config, "PROJECT_URL", "")
    match = re.match(r"https?://github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$", str(url or "").strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def running_version(text=None):
    """(major, minor, patch, is_full_release) for this bot's SCRIPT_VERSION, or
    None. "DCCore v1.14.0-RC1" is (1, 14, 0, 0): below v1.14.0's (1, 14, 0, 1)."""
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)(\S*)",
                      str(text if text is not None else getattr(config, "SCRIPT_VERSION", "")))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 0 if match.group(4) else 1)


def release_version(tag):
    """The same key for a release TAG, or None for anything but plain
    vMAJOR.MINOR.PATCH - a pre-release or a tag it cannot read is never news."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(tag or "").strip())
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 1) if match else None


def fetch_latest(opener=None):
    """{"tag", "url", "prerelease"} for the latest release, or CheckFailed.

    `opener` is urllib.request.urlopen unless a test gives another: no test in
    this project may reach the network."""
    repo = repository()
    if not repo:
        raise CheckFailed("PROJECT_URL is not a GitHub repository address")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"DCCore version check ({config.SCRIPT_VERSION})"})
    opener = opener or urllib.request.urlopen
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(256 * 1024)
    except urllib.error.HTTPError as err:
        if err.code == 403 and str(err.headers.get("X-RateLimit-Remaining", "")) == "0":
            raise CheckFailed("GitHub's hourly limit for unauthenticated requests was reached") from None
        if err.code == 404:
            raise CheckFailed(f"GitHub has no published release for {repo}") from None
        raise CheckFailed(f"GitHub answered HTTP {err.code}") from None
    except urllib.error.URLError as err:
        raise CheckFailed(f"could not connect to GitHub ({err.reason})") from None
    except (OSError, ValueError) as err:
        # A timeout (socket.timeout is an OSError) or a TLS failure.
        raise CheckFailed(f"could not connect to GitHub ({err})") from None
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CheckFailed("GitHub's answer could not be read") from None
    if not isinstance(data, dict) or not data.get("tag_name"):
        raise CheckFailed("GitHub's answer named no release")
    return {"tag": str(data["tag_name"]).strip(),
            "url": str(data.get("html_url") or f"https://github.com/{repo}/releases"),
            "prerelease": bool(data.get("prerelease") or data.get("draft"))}


def _current_text():
    key = running_version()
    return "v%d.%d.%d" % key[:3] if key else str(getattr(config, "SCRIPT_VERSION", "?"))


def state():
    """Everything the dashboard and the console show, from runtime.py."""
    return {
        "enabled": bool(getattr(config, "CHECK_FOR_UPDATES", True)),
        "current": _current_text(),
        "latest": runtime.update_check_latest,
        "url": runtime.update_check_url,
        "newer": bool(runtime.update_check_newer),
        "checked_at": runtime.update_check_at,
        "error": runtime.update_check_error,
        "error_at": runtime.update_check_error_at,
    }


def check(manual=False, now=None, fetch=None):
    """Ask GitHub now, record the outcome, say it where the operator looks.

    A failure is said every time it happens - once a day at most for the daily
    check - and stays in state() until a check succeeds. A newer release is
    said once per release, and stays in state()."""
    import announce

    now = time.time() if now is None else now
    runtime.update_check_last_attempt = now
    try:
        latest = (fetch or fetch_latest)()
    except CheckFailed as err:
        runtime.update_check_error = str(err)
        runtime.update_check_error_at = now
        tail = "" if manual else " Trying again tomorrow."
        announce.send_debug(f"Could not check for a new version of DCCore: {err}.{tail}", category="INFO")
        return state()

    runtime.update_check_error = None
    runtime.update_check_error_at = None
    runtime.update_check_at = now
    mine = running_version()
    theirs = None if latest["prerelease"] else release_version(latest["tag"])
    runtime.update_check_latest = latest["tag"] if theirs else None
    runtime.update_check_url = latest["url"]
    runtime.update_check_newer = bool(mine and theirs and theirs > mine)
    if runtime.update_check_newer and runtime.update_check_announced != latest["tag"]:
        runtime.update_check_announced = latest["tag"]
        announce.send_debug(f"A new version of DCCore is available: {latest['tag']} "
                            f"(this bot runs {_current_text()}) - {latest['url']}", category="INFO")
    return state()


def manual_check(now=None, fetch=None):
    """Check now for the operator, whatever CHECK_FOR_UPDATES says - but not
    twice inside MANUAL_COOLDOWN_SECONDS: GitHub allows 60 unauthenticated
    requests an hour per address, and a stuck button should not spend them.
    Inside the cooldown the last outcome is returned with "cooldown": seconds."""
    now = time.time() if now is None else now
    last = runtime.update_check_last_manual
    if last is not None and now - last < MANUAL_COOLDOWN_SECONDS:
        result = state()
        result["cooldown"] = int(MANUAL_COOLDOWN_SECONDS - (now - last)) + 1
        return result
    runtime.update_check_last_manual = now
    return check(manual=True, now=now, fetch=fetch)


def describe(now=None):
    """One line for the console's `status`."""
    info = state()
    if info["error"]:
        when = time.strftime("%H:%M", time.localtime(info["error_at"])) if info["error_at"] else "?"
        return f"could not check ({info['error']}) - last tried {when}"
    if info["newer"]:
        return f"{info['latest']} available - {info['url']}"
    if info["checked_at"]:
        return f"up to date ({info['current']})"
    return f"{info['current']} - not checked yet" if info["enabled"] else f"{info['current']} - daily check off"


def due(now):
    """Whether the daily check wants to run at `now`."""
    if not getattr(config, "CHECK_FOR_UPDATES", True):
        return False
    last = runtime.update_check_last_attempt
    return last is None or now - last >= CHECK_INTERVAL_SECONDS


def worker(sleep=None):
    """The loop: a wait for the bot to connect, then a look every ten minutes
    at whether a day has passed. Turning the setting off idles it - due()
    reads it every time."""
    naptime = sleep or (lambda seconds: time.sleep(seconds))
    naptime(FIRST_CHECK_DELAY_SECONDS)
    while True:
        try:
            if due(time.time()):
                check()
        except Exception as err:
            print(f"[UPDATE] The version check failed unexpectedly: {err}")
        naptime(WORKER_TICK_SECONDS)


def ensure_worker(start=None):
    """Start the loop once per process when CHECK_FOR_UPDATES is on - from
    oserve.startup() AND from every rehash, so ticking it on on the Settings
    page (a save fires a rehash) starts it without a restart. The pattern of
    the automatic list refresh and the rebuild schedule: ONCE, guarded in
    runtime.py. Off, nothing is started - which is also what keeps any test
    that boots the daemon from holding a thread that might one day reach
    GitHub. `start` is injectable for tests."""
    if not getattr(config, "CHECK_FOR_UPDATES", True):
        return False
    with runtime.update_check_guard:
        if runtime.update_check_started:
            return False
        starter = start or (lambda: threading.Thread(target=worker, daemon=True).start())
        starter()
        runtime.update_check_started = True
    return True
