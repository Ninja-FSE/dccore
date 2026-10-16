"""The first-run setup page (#1322): the questions a new install answers in
the browser, in the operator's language, their validation, writing the
answers, and the two pages it shows.

Split out of webserver.py, step 6 of splitting it (docs/FUTURE.md);
webserver's setup server still serves these and imports every name back.
"""

import os

from web_settings import _settings_field
import adminchat
import defaults as config
from web_common import WEB_DIR


# ---------------------------------------------------------------------------
# SET IT UP IN THE BROWSER (#547, Proposal 4)
#
# configure.py's questions are right; the terminal is the wrong place for a
# first-timer to answer them, on any OS. With no configuration at all -
# settings_file.REQUIRED still blank - oserve.startup() used to exit 1 with a
# message about admin_config.py.sample. Now, when Flask is present (Proposal
# 1's launchers make sure of that before the daemon starts), it serves ONE
# page on 127.0.0.1 until the form below has written settings.conf and
# admin_config.py exactly as configure.py writes them, then carries on down
# the same straight line it always ran. Same process, same port, freed and
# re-bound by the real dashboard a moment later; no launcher involvement.
#
# What makes it safe to run with no password yet:
#   - loopback only, never WEBUI_HOST (nothing is configured to read);
#   - it exists only while there is no configuration - the moment the form
#     has written one, the server stops, and the real app's login gate is
#     what answers on that port from then on;
#   - a ONE-TIME TOKEN. Loopback alone is not "only the person at the
#     machine": any website open in the same browser can POST to
#     127.0.0.1:8420, and with no password yet that POST would set ITS
#     password. So the token is printed in the terminal, put in the URL the
#     launcher opens, required on every request, and never in a form action
#     a page from another origin could guess; the Host header is checked
#     too, against DNS rebinding.
#
# The form is the Settings page's own field machinery (_settings_field), so
# the plain-language help from #545 and the fr/es strings from the lang
# files appear here without a second copy of either.
# ---------------------------------------------------------------------------

SETUP_FIELDS = ("NICKNAME", "SERVER", "CHANNEL", "ADMIN_NICK", "FILE_DIRECTORY",
                "WEBUI_ENABLED", "WEBUI_HOST", "CHECK_FOR_UPDATES")


SETUP_LANGS = ("en", "fr", "es")


_SETUP_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def _setup_strings(lang):
    """The lang file's strings for the setup page, or {} for English -
    English is what SETTINGS_LABELS and settings_help carry already."""
    lang = lang if lang in SETUP_LANGS else "en"
    if lang == "en":
        return {}
    path = os.path.join(WEB_DIR, "lang", f"{lang}.json")
    try:
        import json
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def build_setup_fields(lang="en", values=None):
    """The form's fields, from the same _settings_field() the Settings page
    uses, with the lang file's label and help laid over for fr/es. `values`
    are what the operator typed, redisplayed after an error."""
    import settings_file
    strings = _setup_strings(lang)
    types = settings_file.declared_types(vars(config))
    # A fresh page (nothing typed yet) starts with the dashboard box TICKED.
    # defaults.py ships WEBUI_ENABLED = False (convention 1: a listener ships
    # off), and read as-is that unticked the box on every first run - while
    # the intro, the folder placeholder and the folder error all defer the
    # music folder to "the dashboard's Settings page" (#603). A novice who
    # took that advice and did not notice the box ended with a bot that
    # served nothing and had no Settings page to fix it from. This page
    # exists only because Flask is installed - the launcher just installed
    # it for the dashboard - and the box still binds loopback unless the LAN
    # box is ticked too; unticking it stays one click. A redisplay after an
    # error keeps what the operator chose: an unticked box is simply absent
    # from the POST, so `values` then lacks the key and config's False wins.
    fresh = values is None
    values = values or {}
    fields = []
    for name in SETUP_FIELDS:
        current = values.get(name, getattr(config, name, None))
        if name in ("WEBUI_ENABLED", "CHECK_FOR_UPDATES"):
            # Both start ticked: the operator sees the choice and can untick
            # it (#572 - the daily version check is on by default, visibly).
            # On a redisplay the box is what was sent - absent is unticked,
            # never config's value: CHECK_FOR_UPDATES ships True, and falling
            # back to it re-ticked a box the operator had just unticked.
            current = True if fresh else str(values.get(name, "")).lower() in ("1", "on", "true", "yes")
        field = _settings_field(name, types.get(name, str), current)
        label = strings.get(f"settings.field.{name}")
        if label:
            field["label"] = label
        help_text = strings.get(f"settings.field.{name}.help")
        if help_text:
            field["help"] = help_text
        fields.append(field)
    return fields


def validate_setup_form(form, lang="en"):
    """The form's answers -> (changes, password_hash, errors). `changes` is
    the same {NAME: value} dict configure.collect_answers() builds - what
    was answered is written, what was left blank is not - so the files the
    page writes are the files the terminal writes. The error messages come
    from the lang file for fr/es, like the rest of the page (#621); the
    nickname problem itself (settings_file.nick_problem) stays English."""
    import settings_file
    strings = _setup_strings(lang)
    errors = []
    changes = {}

    def text(name):
        return str(form.get(name, "") or "").strip()

    def say(key, english):
        return strings.get(key, english)

    def not_a_nick(problem):
        return say("setup.error.nickname_invalid",
                   "That is not an IRC nickname: {problem}.").replace("{problem}", problem)

    nickname = text("NICKNAME")
    if not nickname:
        errors.append(("NICKNAME", say("setup.error.nickname_needed", "A nickname is needed.")))
    elif settings_file.nick_problem(nickname):
        errors.append(("NICKNAME", not_a_nick(settings_file.nick_problem(nickname))))
    else:
        changes["NICKNAME"] = nickname

    server = text("SERVER")
    if not server:
        errors.append(("SERVER", say("setup.error.server_needed",
                                     "An IRC server is needed (irc.undernet.org is "
                                     "the usual one).")))
    elif settings_file.server_problem(server):
        # ":" and "/" as well as a space (#687): "irc.undernet.org:6667" and
        # a pasted irc:// URL were accepted and the bot looped on
        # getaddrinfo every ten seconds with nothing saying why.
        errors.append(("SERVER", say("setup.error.server_shape",
                                     "A server name has no spaces, no port and no irc:// - "
                                     "just the host, e.g. irc.undernet.org (the port is a "
                                     "separate setting).")))
    else:
        changes["SERVER"] = server

    channel = text("CHANNEL")
    channels = [c.strip() for c in channel.split(",") if c.strip()]
    if not channels:
        errors.append(("CHANNEL", say("setup.error.channel_needed",
                                      "At least one channel is needed, like #mychannel.")))
    elif settings_file.channels_problem(channel):
        # settings_file's rule, not a copy of it (#1272): configure.py and the
        # Settings page accepted what this refused, so "the files the page
        # writes are the files the terminal writes" was not true of CHANNEL.
        errors.append(("CHANNEL", say("setup.error.channel_shape",
                                      "Each channel starts with # and has no spaces; "
                                      "separate several with commas.")))
    else:
        changes["CHANNEL"] = ",".join(channels)

    admin_nick = text("ADMIN_NICK")
    if not admin_nick:
        errors.append(("ADMIN_NICK", say("setup.error.admin_needed",
                                         "Your own nick is needed - the person who may "
                                         "run the admin commands.")))
    elif settings_file.nicks_problem(admin_nick):
        errors.append(("ADMIN_NICK", not_a_nick(settings_file.nicks_problem(admin_nick))))
    else:
        changes["ADMIN_NICK"] = admin_nick

    # Optional (#811): not a real setting name, wrapped into ADMIN_HOSTMASKS
    # below. Blank is a supported, unremarkable answer - it leaves the
    # console exactly as unconfigured as it always shipped.
    admin_host = text("ADMIN_HOST")
    if admin_host:
        problem = settings_file.admin_host_problem(admin_host)
        if problem:
            errors.append(("ADMIN_HOST", say("setup.error.admin_host_shape",
                                             "That is not a services host: {problem}.").replace("{problem}", problem)))
        else:
            changes["ADMIN_HOSTMASKS"] = [f"*!*@{admin_host}"]

    password = str(form.get("password", "") or "")
    confirm = str(form.get("password_confirm", "") or "")
    password_hash = None
    if not password:
        errors.append(("password", say("setup.error.password_needed",
                                       "A password is needed - it opens the admin "
                                       "console and this dashboard.")))
    elif password != confirm:
        errors.append(("password", say("setup.error.password_mismatch",
                                       "The two passwords do not match.")))
    else:
        password_hash = adminchat.make_password_hash(password)

    enable_webui = str(form.get("WEBUI_ENABLED", "") or "").lower() in ("1", "on", "true", "yes")

    file_directory = text("FILE_DIRECTORY")
    if file_directory:
        if os.path.isdir(file_directory):
            changes["FILE_DIRECTORY"] = file_directory
        else:
            # "Later on the Settings page" is only true with the dashboard on;
            # with the box unticked the folder can only be set in the file (#603).
            errors.append(("FILE_DIRECTORY",
                           say("setup.error.folder_missing",
                               "That folder does not exist. Leave it blank to choose "
                               "it later on the dashboard's Settings page.")
                           if enable_webui else
                           say("setup.error.folder_missing_no_dashboard",
                               "That folder does not exist. With the dashboard off, "
                               "leave it blank and set FILE_DIRECTORY in settings.conf "
                               "later, or tick the dashboard box to choose it on its "
                               "Settings page.")))

    changes["WEBUI_ENABLED"] = enable_webui
    # #572: ticked unless the operator unticked it; a box left unticked is
    # not sent at all, which is the "off".
    changes["CHECK_FOR_UPDATES"] = str(form.get("CHECK_FOR_UPDATES", "") or "").lower() in ("1", "on", "true", "yes")
    if enable_webui:
        lan = str(form.get("WEBUI_LAN", "") or "").lower() in ("1", "on", "true", "yes")
        changes["WEBUI_HOST"] = "0.0.0.0" if lan else "127.0.0.1"

    return changes, password_hash, errors


def apply_setup(changes, password_hash, log=print, settings_path=None, admin_path=None):
    """Write both files as configure.py does, then make the running process
    see them: settings.conf through settings_file.apply_to(), the password
    by hand - admin_config.py was imported (or not) long before this and
    is not re-imported. The two paths are for tests; a real run writes
    where configure.py writes."""
    import configure
    import settings_file
    # admin_config.py FIRST. The two writes are not one transaction, and
    # only one order fails safe: once settings.conf carries NICKNAME, CHANNEL
    # and ADMIN_NICK the REQUIRED gate is satisfied, so if it were written
    # first and the password write then failed (the file held open by an
    # editor or a scanner, a full disk), a restart would skip this page,
    # join IRC and refuse the dashboard for the missing hash - with no way
    # back to the form. A hash written before settings.conf hurts nothing:
    # the gate still trips, the page is offered again and the next attempt
    # replaces the line in place (#624).
    # The writer returns the settings.conf path when THAT file also sets
    # ADMIN_PASSWORD_HASH (#676, audit L12): defaults.py applies it after
    # admin_config.py, so after a restart the hash written here loses to
    # it. The writer prints the warning to the daemon's window; the person
    # at the form is in a browser and never saw it - so it is returned, and
    # the saved page says it too.
    # BUT ONLY ONCE settings.conf IS KNOWN TO TAKE THE SAVE (#1272). A
    # settings.conf with one malformed line cannot be edited, so the second
    # write failed after the first had already replaced ADMIN_PASSWORD_HASH:
    # the page showed a write error and the password had changed anyway.
    # check_save() runs every check save() runs and writes nothing, so a save
    # that is going to fail fails here, with both files untouched.
    settings_file.check_save(vars(config), changes, path=settings_path)
    shadow = configure.write_admin_config_password(password_hash, path=admin_path)
    configure.write_settings_conf(changes, path=settings_path)
    settings_file.apply_to(vars(config), path=settings_path, log=log)
    config.ADMIN_PASSWORD_HASH = password_hash
    # apply_to() assigned NICKNAME but did not re-run the derivations that
    # depend on it; the list rebuild is a new process and does (#590).
    config.derive_list_base_name()
    return {"written": sorted(changes),
            "shadowed_by": os.path.basename(shadow) if shadow else None}


def _setup_host_ok(host_header):
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host in _SETUP_HOSTS


def _html(text):
    import html
    return html.escape(str(text if text is not None else ""), quote=True)


def render_setup_page(fields, token, lang="en", errors=(), values=None, port=8420):
    """The one page. Server-rendered - the real dashboard's app.js is behind
    the login and is not loaded here; the page has no script but the
    language switch, which is a link."""
    strings = _setup_strings(lang)
    errors = dict(errors)
    values = values or {}
    rows = []
    for field in fields:
        name = field["name"]
        label = _html(field["label"])
        help_text = field.get("help") or ""
        help_html = (f' <span class="help" title="{_html(help_text)}">?</span>'
                     if help_text else "")
        error_html = (f'<div class="error">{_html(errors[name])}</div>'
                      if name in errors else "")
        value = values.get(name, field.get("value"))
        if name in ("WEBUI_ENABLED", "CHECK_FOR_UPDATES"):
            checked = " checked" if (value is None or value in (True, "on", "1", "true")) else ""
            rows.append(f'<label class="check"><input type="checkbox" name="{name}" value="1"{checked}> '
                        f'{label}{help_html}</label>')
            continue
        if name == "WEBUI_HOST":
            lan = str(value or "") == "0.0.0.0" or values.get("WEBUI_LAN") in ("1", "on")
            checked = " checked" if lan else ""
            rows.append(f'<label class="check sub"><input type="checkbox" name="WEBUI_LAN" value="1"{checked}> '
                        f'{_html(strings.get("setup.lan", "Reachable from other devices on my network (phone, laptop), not only this machine"))}'
                        f'{help_html}</label>')
            continue
        placeholder = ""
        if name == "SERVER" and not value:
            value = "irc.undernet.org"
        if name == "CHANNEL":
            placeholder = ' placeholder="' + _html(strings.get("setup.channel_placeholder", "#mychannel, #another")) + '"'
        if name == "FILE_DIRECTORY":
            placeholder = ' placeholder="' + _html(strings.get("setup.folder_placeholder", "optional - can be chosen later on the Settings page")) + '"'
        rows.append(f'<label>{label}{help_html}<input type="text" name="{name}" '
                    f'value="{_html(value or "")}"{placeholder} autocomplete="off"></label>{error_html}')
        if name == "ADMIN_NICK":
            # Not a real setting field (#811): the operator types the host
            # alone, and validate_setup_form() wraps it as ADMIN_HOSTMASKS =
            # ["*!*@<host>"]. Optional, so no placeholder value is offered -
            # a blank here is a real, supported "skip it" answer, not an
            # unanswered required field.
            admin_host_error = (f'<div class="error">{_html(errors["ADMIN_HOST"])}</div>'
                                if "ADMIN_HOST" in errors else "")
            rows.append(
                f'<label>{_html(strings.get("setup.admin_host", "Your services host (optional)"))}'
                f' <span class="help" title="{_html(strings.get("setup.admin_host_help", "Locks the admin console, and the in-channel admin commands once this is set, to your account rather than just your nick. Log into services, set +x, then /whois yourself for the host - looks like yourname.users.undernet.org."))}">?</span>'
                f'<input type="text" name="ADMIN_HOST" value="{_html(values.get("ADMIN_HOST", ""))}" '
                f'placeholder="{_html(strings.get("setup.admin_host_placeholder", "optional - blank leaves the console open to the nick alone"))}" '
                f'autocomplete="off"></label>{admin_host_error}')
    password_error = f'<div class="error">{_html(errors["password"])}</div>' if "password" in errors else ""
    rows.append(f'<label>{_html(strings.get("setup.password", "Admin password"))}'
                f' <span class="help" title="{_html(strings.get("setup.password_help", "Opens the admin console and this dashboard. Kept as a hash, never in clear."))}">?</span>'
                f'<input type="password" name="password" autocomplete="new-password"></label>')
    rows.append(f'<label>{_html(strings.get("setup.password_again", "The same password again"))}'
                f'<input type="password" name="password_confirm" autocomplete="new-password"></label>{password_error}')
    switch = " ".join(
        f'<a href="/setup?token={_html(token)}&amp;lang={code}"{" class=on" if code == lang else ""}>{code.upper()}</a>'
        for code in SETUP_LANGS)
    from string import Template
    return Template(SETUP_PAGE).substitute(
        title=_html(strings.get("setup.title", "Set up DCCore")),
        intro=_html(strings.get("setup.intro", "A few questions and the bot can start. Everything else is on the dashboard's Settings page afterwards, behind the password you choose here.")),
        rows="\n".join(rows), token=_html(token), lang=_html(lang), switch=switch,
        submit=_html(strings.get("setup.submit", "Save and start the bot")),
        note=_html(strings.get("setup.note", "Only this machine can reach this page, and only until the settings are saved.")))


def render_setup_saved_page(changes, lang="en", port=8420, shadowed_by=None):
    strings = _setup_strings(lang)
    dashboard = bool(changes.get("WEBUI_ENABLED"))
    from string import Template
    warning = ""
    if shadowed_by:
        # The password just chosen works until the next restart, and then
        # the hash in settings.conf wins (#676). Said on the page, where the
        # person who chose it is.
        text = strings.get("setup.saved.shadowed",
                           "{file} also sets ADMIN_PASSWORD_HASH, and it is applied after admin_config.py - "
                           "so after the next restart the password you just chose will stop working. "
                           "Remove the ADMIN_PASSWORD_HASH line from {file}, or change the password from "
                           "the dashboard, which writes to that file.")
        warning = '<p class="warn">%s</p>' % _html(text.replace("{file}", str(shadowed_by)))
    return Template(SETUP_SAVED_PAGE).substitute(
        title=_html(strings.get("setup.saved.title", "Saved - starting the bot")),
        body=_html(strings.get("setup.saved.dashboard" if dashboard else "setup.saved.no_dashboard",
                               "The bot is starting. This page opens the dashboard's login as soon as it answers - log in with the password you just chose."
                               if dashboard else
                               "The bot is starting. You chose no dashboard, so this page has nothing more to show: the bot's window is where it reports from now on. Close this tab.")),
        warning=warning,
        poll="true" if dashboard else "false", port=str(port))


SETUP_PAGE = """<!doctype html>
<html lang="$lang"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
  body { font-family: -apple-system, "Segoe UI", sans-serif; background: #0b0f12; color: #e6edf0; margin: 0; padding: 2rem 1rem; }
  form { background: #131a1f; padding: 1.75rem 2rem 1.5rem; border-radius: 10px; max-width: 34rem; margin: 0 auto; box-shadow: 0 4px 24px rgba(0,0,0,0.4); }
  h1 { font-size: 1.15rem; margin: 0 0 0.5rem; font-weight: 600; }
  p.intro { color: #9fb0ba; font-size: 0.9rem; margin: 0 0 1.25rem; line-height: 1.45; }
  label { display: block; font-size: 0.85rem; margin-bottom: 0.9rem; color: #c9d4da; }
  label.check { display: flex; align-items: center; gap: 0.5rem; }
  label.check.sub { margin-left: 1.6rem; }
  input[type=text], input[type=password] { display: block; width: 100%; padding: 0.55rem 0.6rem; margin-top: 0.3rem; box-sizing: border-box;
    background: #0b0f12; border: 1px solid #2a343b; border-radius: 6px; color: #e6edf0; font-size: 0.95rem; }
  input[type=checkbox] { accent-color: #2dd4c8; }
  .help { display: inline-block; width: 1.1rem; height: 1.1rem; line-height: 1.1rem; text-align: center; border-radius: 50%; background: #23303a; color: #9fd8d3; font-size: 0.75rem; margin-left: 0.35rem; cursor: help; }
  .error { color: #f87171; font-size: 0.82rem; margin: -0.5rem 0 0.9rem; }
  button { width: 100%; padding: 0.65rem; background: #2dd4c8; color: #06231f; border: 0; border-radius: 6px; font-weight: 600; font-size: 0.95rem; cursor: pointer; margin-top: 0.5rem; }
  .langs { text-align: right; font-size: 0.75rem; margin-bottom: 0.5rem; }
  .langs a { color: #9fb0ba; text-decoration: none; margin-left: 0.6rem; }
  .langs a.on { color: #2dd4c8; font-weight: 600; }
  p.note { color: #6b7c86; font-size: 0.75rem; margin: 1rem 0 0; }
</style></head>
<body>
  <form method="post" action="/setup">
    <div class="langs">$switch</div>
    <h1>$title</h1>
    <p class="intro">$intro</p>
    <input type="hidden" name="token" value="$token">
    <input type="hidden" name="lang" value="$lang">
$rows
    <button type="submit">$submit</button>
    <p class="note">$note</p>
  </form>
</body></html>"""


SETUP_SAVED_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>$title</title>
<style>
  body { font-family: -apple-system, "Segoe UI", sans-serif; background: #0b0f12; color: #e6edf0; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
  div { background: #131a1f; padding: 2rem 2.25rem; border-radius: 10px; max-width: 30rem; box-shadow: 0 4px 24px rgba(0,0,0,0.4); }
  h1 { font-size: 1.05rem; margin: 0 0 0.75rem; font-weight: 600; }
  p { color: #9fb0ba; font-size: 0.9rem; line-height: 1.5; margin: 0; }
  p.warn { color: #fbbf24; margin-top: 0.9rem; }
</style></head>
<body><div><h1>$title</h1><p>$body</p>$warning</div>
<script>
  if ($poll) {
    setInterval(function () {
      fetch("/login", {cache: "no-store"}).then(function (r) {
        if (r.ok) { window.location.href = "/login"; }
      }).catch(function () {});
    }, 2000);
  }
</script>
</body></html>"""
