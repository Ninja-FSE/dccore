# webserver.py - Web dashboard (Search / Queue / File Lists / cross-bot fetch).
"""A small, optional status page for the daemon.

FLASK IS OPTIONAL. The daemon has no external dependencies today, and CI never
installs Flask - so importing this module must never fail, and starting the
dashboard when Flask is missing must log and return, never crash the daemon.
That is why the import is guarded and why HAVE_FLASK exists.

The build_*_payload()/start_broadcast_search()/build_fetch_*() functions below
are pure: they read/write config (and, for search/file-lists, the master list
via list.py) and return plain dicts/lists or (status, dict) tuples. They never
import or touch flask, which is what lets tests/test_webserver.py exercise the
real data-shaping and mutation logic with plain unittest, no Flask install
required - keeping the "stdlib-only" property the rest of the test suite
relies on. create_app() and start() are the only things gated on HAVE_FLASK.

EVERY ROUTE REQUIRES A LOGIN, INCLUDING STATIC ASSETS. The dashboard used to
run with no authentication at all - a deliberate LAN-only decision, extended
on purpose even to /api/search/broadcast and /api/fetch/* despite those
routes mutating state (queuing an outbound IRC line, dialling an IP:port a
foreign bot supplies). That changed because WEBUI_HOST is no longer
guaranteed to stay LAN-only in practice: it shares one password with the DCC
CHAT admin console (config.ADMIN_PASSWORD_HASH, generated with `python
src/adminchat.py`) rather than a second credential to configure and forget about.
start() now refuses to run at all when that hash is unset - see the check
near the bottom of this file - so the dashboard is never reachable
unauthenticated, not even briefly on a fresh install.

The login route ("/login") is the ONLY exemption from the require_login()
before_request hook below; everything else, static files included, is behind
it. The session is a signed Flask cookie (app.secret_key, generated
fresh per process) - it does not survive a daemon restart, which is a
deliberate simplification: re-logging in after a restart costs nothing, and
it avoids a second secret to persist and protect. The cookie is not the
whole session, though: it carries an id the server must still hold (a
logout drops it) and a stamp of the password it was opened with (a password
change ends every session opened with the old one) - see create_app(). This is real
authentication, not the workaround kind the module docstring used to warn
against (an API key in a query string, a cookie nobody checks) - the password
is verified against ADMIN_PASSWORD_HASH via adminchat.verify_password() on
every login attempt, and nothing downstream trusts a request that has not
passed require_login(). Repeated failures from one address are temporarily
blocked (_note_bad_web_login()/_is_bad_web_ip(), same attempt-count and
block-duration policy as adminchat.py's own DCC CHAT console tracker, reused
from there directly) - in a POOL SEPARATE FROM adminchat.py's, on purpose:
since the password is shared, a shared block budget would let a web attacker
spend it down and lock the real operator out of the DCC console too. A POST
whose Origin (or Referer) names another site is refused unread and uncounted
(_login_origin_ok(), #609): on the loopback install a hostile page in the
operator's own browser arrives from the same address as the operator. An
attempt is claimed before its password is hashed (_reserve_web_login()), so
parallel requests from one address get no more guesses than the limit. A
request for a host name the dashboard does not answer to (DNS rebinding) is
refused before anything else (_dashboard_host_ok()), and no response may be
shown inside another page's frame (_refuse_framing()).

What plain-HTTP session/password transmission still cannot fix: an attacker
already sharing the network segment can read the password and the session
cookie off the wire. The WEBUI_HOST comment in config.py's warning against
untrusted networks is about exactly that, and still applies with auth in
place - a password gate stops a stranger from finding the dashboard and
using it, not from a wire-level eavesdropper on a network the operator should
not have put this host on to begin with.

Nothing here is added to commands.py's CORE_MODULES. A !rehash reload
re-executes this module's body, which would try to re-bind a live listening
socket out from under app.run() - the same reasoning that already excludes
adminchat.py. Route handlers read config fresh via getattr() on every request
instead, so a rehash's new values (e.g. a changed WEBUI_* setting takes effect
only on the next daemon restart, but MAX_DCC_SLOTS, the queue, etc. are always
current) are visible without needing a reload of this module.
"""

import collections
import os
import sys
import threading
import time

import adminchat
import defaults as config
import platform_compat
import runtime


try:
    from flask import (Flask, Response, g, jsonify, redirect, request, send_file,
                       send_from_directory, session)
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False


# The shared limits and helpers live in web_common.py since #1311, the
# first step of splitting this file. Imported back by name, so
# webserver.<name> still answers for every one of them.
from web_common import (  # noqa: F401
    FETCH_ENQUEUE_MAX_ITEMS,
    FILELISTS_DEFAULT_PAGE_SIZE,
    FILELISTS_MAX_PAGE_SIZE,
    FOLDER_BROWSE_MAX_ENTRIES,
    IRC_LINE_FIELD_MAX_LEN,
    MAX_FOLDER_PATH_LEN,
    MAX_SERVED_FOLDERS,
    WEBUI_MAX_SEARCH_RESULTS,
    WEB_DASHBOARD_SOURCE,
    WEB_DIR,
    json_object,
    parse_pagination_params,
    reject_if_unsafe_for_irc_line,
    split_list_search_words,
)


# This section is web_library.py since #1318, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_library import (  # noqa: F401
    _format_library_size,
    _rar_counts,
    build_filelists_payload,
    build_library_payload,
    build_search_payload,
    count_rar_album_folders,
)








# This section is web_stats.py since #1318, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_stats import (  # noqa: F401
    MAX_IMPORT_BYTES,
    MAX_IMPORT_FILES,
    MAX_IMPORT_SPEED,
    MAX_KTDATA_CHARS,
    RECORD_PERIODS,
    STATS_PARTS,
    _FORMULA_START,
    _IMPORT_LIMITS,
    _RECEIVED,
    _csv_cell,
    _lifetime_sent,
    _nick_rows,
    _read_ktdata_text,
    _record_off,
    _record_outcomes,
    _record_since,
    _record_tops,
    _wait_text,
    apply_ktdata_import,
    apply_record_forget,
    apply_stats_import,
    build_ktdata_preview,
    build_record_nick_payload,
    build_record_payload,
    build_stats_import_preview,
    build_stats_payload,
    current_importable_stats,
    record_csv_lines,
    validate_import_values,
)



















































# This section is web_queue.py since #1318, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_queue import (  # noqa: F401
    _pack_fields,
    _queue_nick,
    _queue_row_id,
    _security_seconds_left,
    build_ignore_result,
    build_pack_cancel_result,
    build_queue_clear_result,
    build_queue_move_file_result,
    build_queue_move_user_result,
    build_queue_payload,
    build_queue_remove_file_result,
    build_unignore_result,
)






























# This section is web_lists.py since #1315, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_lists import (  # noqa: F401
    BOT_ALONE_FETCH_CONFLICT_ERROR,
    OWN_SOURCE,
    PACK_FETCH_CONFLICT_ERROR,
    _ADVERT_LIVE_FIELDS,
    _MAX_SAFE_JS_INT,
    _advert_live,
    _advert_now,
    _display_nick,
    _enqueue_folder_request,
    _fetched_list_entry,
    _freshness,
    _ident_merges,
    _looks_like_a_nick,
    _preferred_fetch_channel,
    bot_not_here_error,
    build_add_source_result,
    build_crosslist_search_payload,
    build_fetched_bot_list_payload,
    build_fetched_bot_list_summaries,
    build_folder_rar_batch_enqueue_result,
    build_folder_rar_fetch_enqueue_result,
    build_list_fetch_enqueue_result,
    build_own_list_summaries,
    build_purge_all_fetched_lists_result,
    build_purge_offline_fetched_lists_result,
    build_remove_source_result,
    fetch_feature_error,
    fetch_marks_by_bot,
    held_list_channel,
    mark_rows_with_fetch_state,
    own_list_name,
    own_list_source,
    present_nicks,
    requested_own_list,
    source_is_ours,
)








































































# This section is web_search.py since #1313, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_search import (  # noqa: F401
    BROADCAST_SEARCH_MIN_TERM_LEN,
    BROADCAST_SEARCH_WINDOW,
    build_broadcast_status_payload,
    start_broadcast_search,
)








# This section is web_fetch.py since #1317, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_fetch import (  # noqa: F401
    FETCH_CLEAR_STATES,
    build_fetch_clear_result,
    build_fetch_delete_many_result,
    build_fetch_delete_result,
    build_fetch_enqueue_result,
    build_fetch_pause_result,
    build_fetch_status_payload,
    build_fetched_list_purge_result,
)
















# This section is web_list_update.py since #1317, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_list_update import (  # noqa: F401
    build_update_list_status_payload,
    build_verify_list_payload,
    read_list_progress,
    start_audio_info_reading,
    start_list_update,
)












# This section is web_settings.py since #1320, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_settings import (  # noqa: F401
    CHOICE_LABELS,
    EDITED_ELSEWHERE,
    SETTINGS_CATEGORIES,
    SETTINGS_LABELS,
    SETTINGS_RESTART_ONLY,
    SETTINGS_UNITS,
    THEME_PREVIEW_ROLES,
    _save_settings_and_rehash,
    _settings_field,
    _settings_payload_unlocked,
    apply_settings_changes,
    build_password_change_result,
    build_settings_payload,
    build_theme_preview,
    console_is_enabled,
    dashboard_is_loopback_only,
    theme_preview_overrides,
)












# This section is web_messages.py since #1320, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_messages import (  # noqa: F401
    build_messages_payload,
    build_notices_payload,
    mark_messages_read_result,
    mark_notices_read_result,
    messages_are_off,
    messages_payload_or_404,
    messages_read_or_404,
)




















# This section is web_served.py since #1320, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_served import (  # noqa: F401
    MAX_SERVED_LISTS,
    apply_folder_changes,
    apply_list_changes,
    browse_roots,
    build_folder_browse_payload,
    build_folders_payload,
    build_lists_payload,
)








# This section is web_on_connect.py since #1320, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_on_connect import (  # noqa: F401
    apply_on_connect_changes,
    build_on_connect_payload,
    build_on_connect_resend_result,
)




















# This section is web_login.py since #1322, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_login import (  # noqa: F401
    LOGIN_PAGE,
    WEB_MAX_LIVE_SESSIONS,
    WEB_PASSWORD_CHECK_WAIT_SECONDS,
    _check_web_password,
    _clear_bad_web_ip,
    _configured_dashboard_names,
    _dashboard_host_ok,
    _host_name,
    _is_bad_web_ip,
    _login_origin_ok,
    _machine_name_cache,
    _machine_names,
    _note_bad_web_login,
    _note_bad_web_login_locked,
    _origin_netloc,
    _password_stamp,
    _refuse_framing,
    _reserve_web_login,
    _settle_web_login,
    _web_bad_ips,
    _web_bad_ips_lock,
    _web_logins_in_flight,
)






































# This section is web_console.py since #1322, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_console import (  # noqa: F401
    _CONSOLE_UNSUPPORTED_COMMANDS,
    _CONSOLE_UNSUPPORTED_MESSAGES,
    _WebConsoleSession,
    _console_debug_sink,
    _console_log,
    _console_log_lock,
    _ensure_console_sink,
    build_console_command_result,
    build_console_log_payload,
)




# The browser is already on the dashboard's login (#689, audit L25): the
# setup page's "Saved" screen polls /login and navigates there by itself
# as soon as the real app answers, so start() opening the dashboard as well
# gave a first run two tabs, one on /login and one on / (which redirects to
# /login). Set by run_setup_until_configured() when the page it served is
# going to do that; read and cleared by _open_in_browser(), once.
_browser_is_on_the_saved_page = False


def _open_in_browser(host, port, opener=None, log=print):
    """Open the dashboard in the default browser, if that was asked for.

    Not when the setup page's own browser tab is about to arrive here on
    its own (#689): a first run ends on one dashboard tab, not two.

    LOOPBACK ONLY, and not because of security - because of what the machine
    probably is. A dashboard bound to the LAN is as likely to be running on a
    headless box as on somebody's desktop, and a daemon that tries to spawn a
    browser there is doing something nobody asked for and nobody will see.

    Never fatal. A machine with no browser, no display, or a wayward
    BROWSER variable is not a reason to stop the bot starting - the address
    was printed a line above either way.
    """
    global _browser_is_on_the_saved_page
    if _browser_is_on_the_saved_page:
        _browser_is_on_the_saved_page = False
        log("[WEBUI] The setup page's tab opens the login by itself; not opening another.")
        return False
    if not getattr(config, "WEBUI_OPEN_BROWSER", True):
        return False
    if not dashboard_is_loopback_only(host):
        return False
    try:
        import webbrowser

        (opener or webbrowser.open)(f"http://{host}:{port}/")
        return True
    except Exception as err:
        log(f"[WEBUI] Could not open a browser ({err}); the dashboard is "
            f"still running at http://{host}:{port}/")
        return False






def _quiet_the_request_log():
    """Stop werkzeug printing a line per HTTP request onto the operator's
    console, unless DEBUG_MODE asks for it.

    From the beta: "maybe those lines shouldnt be visible on cmd.exe except
    you run dccore on something like debug mode. you miss the important lines
    like search results etc".

    Exactly that. The dashboard polls five endpoints every two seconds, so an
    idle bot with one page open writes on the order of a hundred lines a
    minute:

        127.0.0.1 - - [...] "GET /api/console/log?since=33 HTTP/1.1" 200 -

    Every one of them says the same thing - the dashboard is still open - and
    together they push the lines that matter (a search, a transfer, a
    disconnect) off the screen faster than anyone can read them. The console
    is the operator's only view on a daemon with no window; filling it with
    the dashboard's own heartbeat costs them that view.

    SILENCED, NOT REDIRECTED. Werkzeug's log is a development convenience,
    and every request it reports is one this process just served itself - the
    information is not lost, it was never news. ERROR is left through so a
    genuine failure inside the server still reaches the console.

    Never raises: a logging tweak is not a reason for the dashboard not to
    start.
    """
    if getattr(config, "DEBUG_MODE", False):
        return
    try:
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
    except Exception as err:
        print(f"[WEBUI] Could not quieten the request log: {err}")












if HAVE_FLASK:

    def _keep_json_key_order(app):
        """Turn off Flask's key sorting for every JSON answer (#1143).

        Sorting every key of every dict was most of what /api/fetch/status
        cost to serialise, and the Downloads view polls it every few seconds.
        Nothing reads the keys in sorted order: the page looks fields up by
        name and json.loads gives the same dict either way.

        app.json, the provider object, arrived in Flask 2.2; 2.0 and 2.1 (the
        floor requirements-web.txt allows) read app.config["JSON_SORT_KEYS"]
        instead, which 2.3 dropped. So the setting goes wherever the installed
        Flask looks for it.

        ensure_ascii is left on deliberately. Off, a filename os.listdir()
        returned as a lone surrogate is written out raw, Werkzeug cannot
        encode the body, and the whole route answers 500.
        """
        provider = getattr(app, "json", None)
        if provider is not None and hasattr(provider, "sort_keys"):
            provider.sort_keys = False
        else:
            app.config["JSON_SORT_KEYS"] = False

    def create_app():
        # Flask resolves a relative static_folder against this module's OWN
        # directory (its root_path) - now src/, not the repository root - so
        # this has to be the absolute path WEB_DIR already computed (#959).
        app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")
        app.secret_key = os.urandom(32)
        # The mutating routes (broadcast search, fetch enqueue, list fetch)
        # all POST; Lax is the app's own decision instead of whatever the
        # visitor's browser happens to default to.
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
        # A CEILING ON THE REQUEST BODY. Without one, Flask reads whatever is
        # sent into memory before any route decides what to do with it, and
        # this daemon shares a machine with transfers it must not starve.
        # 8MB is far above every legitimate body here - the largest by a wide
        # margin is a pasted vars.ini, a text file of a few hundred lines -
        # and Flask answers anything past it with 413 rather than buffering
        # it. The login page is behind this too, which is the point: the
        # ceiling applies before authentication, where the daemon has the
        # least reason to trust what it is being handed.
        app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
        _keep_json_key_order(app)

        # SESSIONS THE SERVER CAN END. The cookie alone used to be the whole
        # session - {"authenticated": True}, signed - so a logout only
        # forgot it in the browser that clicked, and a copied cookie stayed
        # good until the daemon restarted. Now each login gets an id that
        # must still be in this table (a logout takes it out), and a stamp
        # of the password it was opened with (_password_stamp) that must
        # still match the current one (a password change ends them all).
        # Oldest dropped first past WEB_MAX_LIVE_SESSIONS: that is a lot of
        # browsers for one operator, and the table must not grow for ever.
        live_sessions = collections.OrderedDict()

        def start_session():
            import secrets
            sid = secrets.token_urlsafe(24)
            with runtime.web_sessions_lock:
                live_sessions[sid] = time.time()
                while len(live_sessions) > WEB_MAX_LIVE_SESSIONS:
                    live_sessions.popitem(last=False)
            # A fresh session, not the one the request came with: nothing
            # set before the login survives into the logged-in one.
            session.clear()
            session["authenticated"] = True
            session["sid"] = sid
            session["pw"] = _password_stamp(
                app.secret_key, getattr(config, "ADMIN_PASSWORD_HASH", ""))

        def session_is_live():
            import hmac
            if not session.get("authenticated"):
                return False
            sid, stamp = session.get("sid"), session.get("pw")
            if not isinstance(sid, str) or not isinstance(stamp, str):
                return False
            current = _password_stamp(app.secret_key,
                                      getattr(config, "ADMIN_PASSWORD_HASH", ""))
            if not hmac.compare_digest(stamp, current):
                return False
            with runtime.web_sessions_lock:
                return sid in live_sessions

        def end_session():
            sid = session.get("sid")
            if isinstance(sid, str):
                with runtime.web_sessions_lock:
                    live_sessions.pop(sid, None)
            session.clear()

        def restamp_session():
            """After this session changed the password: it stays open, every
            other session opened with the old password does not."""
            session["pw"] = _password_stamp(
                app.secret_key, getattr(config, "ADMIN_PASSWORD_HASH", ""))

        @app.after_request
        def deny_framing(response):
            return _refuse_framing(response)

        @app.before_request
        def require_login():
            # FIRST, before the login and before any file: a request for a
            # name this dashboard does not answer to is a DNS-rebinding
            # page, or a name the operator has not configured - refused
            # without reading the password and without counting anything
            # against the address (see _dashboard_host_ok).
            if not _dashboard_host_ok(request.host):
                text = ("This dashboard does not answer to the name {}. Open it by "
                        "its IP address or as localhost, or add the name to "
                        "WEBUI_ALLOWED_HOSTS.").format(_host_name(request.host) or "(none)")
                if request.path.startswith("/api/"):
                    return jsonify({"error": text}), 403
                return Response(text, status=403, mimetype="text/plain")
            # The sole exemption: everything else, static files included,
            # needs a session already marked authenticated by a prior POST
            # here. Endpoint rather than path, so a future route can't
            # accidentally slip past this by sharing a path prefix.
            if request.endpoint == "login":
                return None
            if not session_is_live():
                # A cookie from before a logout or a password change is
                # dropped, so the browser does not keep sending it.
                if session:
                    session.clear()
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Authentication required."}), 401
                return redirect("/login")
            # EVERY POST IS CHECKED AGAINST ITS OWN HOST (#672, audit L8),
            # the way the login's is (#609). SameSite=Lax was the only
            # defence for the routes that take no JSON body - update-list,
            # purge-offline, a source's remove, a fetch's delete, the two
            # read marks - and "site" does not include the port: a plain
            # HTML form on any other local port (a dev server, a NAS UI)
            # submitted them with the operator's cookie attached. A browser
            # sends Origin on every POST, a form's included; a request with
            # neither header (curl, a script) is not a page forwarding
            # another site's form and passes, as at the login.
            if request.method == "POST" and not _login_origin_ok(
                    request.headers.get("Origin"), request.headers.get("Referer"),
                    request.host):
                return jsonify({"error": "This request was sent by another site "
                                         "and was ignored."}), 403
            return None

        @app.route("/login", methods=["GET", "POST"])
        def login():
            error = None
            if request.method == "POST":
                ip = request.remote_addr
                if not _login_origin_ok(request.headers.get("Origin"),
                                        request.headers.get("Referer"),
                                        request.host):
                    # Refused before the password is looked at, and never
                    # counted against the address: a foreign page must not
                    # be able to spend the operator's own attempts (#609).
                    error = ("This login was sent by another site and was "
                             "ignored. Open the dashboard at its own address.")
                    return LOGIN_PAGE.format(
                        error_html='<p class="error">{}</p>'.format(error)), 403
                # The attempt is claimed BEFORE the password is hashed and
                # settled after, so a burst of parallel POSTs from one
                # address gets no more guesses than the limit allows.
                claim = _reserve_web_login(ip)
                if claim == "blocked":
                    error, status = "Too many failed attempts. Try again later.", 401
                elif claim == "busy":
                    error, status = ("Another login from this address is still being "
                                     "checked. Try again in a moment."), 429
                else:
                    outcome = None
                    try:
                        outcome = _check_web_password(
                            getattr(config, "ADMIN_PASSWORD_HASH", ""),
                            request.form.get("password", ""))
                    finally:
                        _settle_web_login(ip, outcome)
                    if outcome:
                        start_session()
                        return redirect("/")
                    if outcome is None:
                        error, status = ("The dashboard is busy checking other logins. "
                                         "Try again in a moment."), 503
                    else:
                        error, status = "Incorrect password.", 401
            error_html = '<p class="error">{}</p>'.format(error) if error else ""
            return LOGIN_PAGE.format(error_html=error_html), (status if error else 200)

        # POST only (#673, audit L9): the page's own button is a form, and a
        # GET that changes state is a link any other site can make the
        # operator's browser follow with the Lax cookie attached - a cross-
        # site navigation to /logout dropped the dashboard to the login form.
        # Ended on the server too: a copy of this session's cookie is
        # refused from now on, not only the browser that clicked.
        @app.route("/logout", methods=["POST"])
        def logout():
            end_session()
            return redirect("/login")

        @app.route("/")
        def index():
            return app.send_static_file("index.html")

        @app.route("/api/queue")
        def api_queue():
            return jsonify(build_queue_payload(user=request.args.get("user")))

        @app.route("/api/queue/pack/cancel", methods=["POST"])
        def api_queue_pack_cancel():
            status, result = build_pack_cancel_result()
            return jsonify(result), status

        @app.route("/api/queue/move-user", methods=["POST"])
        def api_queue_move_user():
            status, result = build_queue_move_user_result(json_object(request.get_json(silent=True)))
            return jsonify(result), status

        @app.route("/api/queue/move-file", methods=["POST"])
        def api_queue_move_file():
            status, result = build_queue_move_file_result(json_object(request.get_json(silent=True)))
            return jsonify(result), status

        @app.route("/api/queue/remove-file", methods=["POST"])
        def api_queue_remove_file():
            status, result = build_queue_remove_file_result(json_object(request.get_json(silent=True)))
            return jsonify(result), status

        @app.route("/api/queue/clear", methods=["POST"])
        def api_queue_clear():
            status, result = build_queue_clear_result(json_object(request.get_json(silent=True)))
            return jsonify(result), status

        @app.route("/api/ignore", methods=["POST"])
        def api_ignore():
            status, result = build_ignore_result(json_object(request.get_json(silent=True)))
            return jsonify(result), status

        @app.route("/api/unignore", methods=["POST"])
        def api_unignore():
            status, result = build_unignore_result(json_object(request.get_json(silent=True)))
            return jsonify(result), status

        @app.route("/api/stats/import/variables")
        def api_stats_import_variables():
            # Served rather than written into app.js so the page's filter and
            # the parser cannot drift: a field added to omenserve_import.FIELDS
            # is kept by the page without the JavaScript changing at all. If
            # they disagreed, the page would strip out a counter the parser
            # was waiting for, and the operator would be told their file has
            # nothing in it.
            import omenserve_import
            return jsonify({"variables": list(omenserve_import.variable_names())})

        @app.route("/api/stats/import/preview", methods=["POST"])
        def api_stats_import_preview():
            # Reads and writes nothing. POST rather than GET because the
            # vars.ini text travels in the body - it is a file's contents,
            # not an identifier.
            body = json_object(request.get_json(silent=True))
            return jsonify(build_stats_import_preview(body.get("text", "")))

        @app.route("/api/stats/import-ktdata/preview", methods=["POST"])
        def api_stats_import_ktdata_preview():
            # KeepTrack's per-nick history (#1064). Reads and writes nothing.
            body = json_object(request.get_json(silent=True))
            status, result = build_ktdata_preview(body.get("text", ""))
            return jsonify(result), status

        @app.route("/api/stats/import-ktdata", methods=["POST"])
        def api_stats_import_ktdata():
            body = json_object(request.get_json(silent=True))
            status, result = apply_ktdata_import(body.get("text", ""))
            return jsonify(result), status

        @app.route("/api/stats/import", methods=["POST"])
        def api_stats_import():
            body = json_object(request.get_json(silent=True))
            status, result = apply_stats_import(body)
            return jsonify(result), status

        @app.route("/api/stats")
        def api_stats():
            # ?parts=transfer is what Live Transfers polls (#1123): the
            # figures it shows, not the files only Stats reads.
            asked = request.args.get("parts")
            if asked is None:
                return jsonify(build_stats_payload())
            parts = [part for part in asked.split(",") if part]
            unknown = sorted(set(parts) - set(STATS_PARTS))
            if unknown or not parts:
                return jsonify({"error": "parts: one or more of " + ", ".join(STATS_PARTS)}), 400
            return jsonify(build_stats_payload(parts))

        @app.route("/api/stats/record")
        def api_stats_record():
            # The transfer record (#1102). Asked for when Stats is opened and
            # when the period changes, never on the page's few-second poll:
            # these are queries over every row.
            status, result = build_record_payload(request.args.get("period", "all"))
            return jsonify(result), status

        @app.route("/api/stats/record/nick")
        def api_stats_record_nick():
            status, result = build_record_nick_payload(request.args.get("nick", ""),
                                                       request.args.get("period", "all"))
            return jsonify(result), status

        @app.route("/api/stats/record/forget", methods=["POST"])
        def api_stats_record_forget():
            status, result = apply_record_forget(json_object(request.get_json(silent=True)))
            return jsonify(result), status

        @app.route("/api/stats/record.csv")
        def api_stats_record_csv():
            period = request.args.get("period", "all")
            since, error = _record_since(period)
            if error:
                return jsonify({"error": error}), 400
            off = _record_off()
            if off:
                return jsonify(off), 409
            # Streamed: a record of years is written out a row at a time.
            return Response(record_csv_lines(since), mimetype="text/csv",
                            headers={"Content-Disposition":
                                     f'attachment; filename="dccore-transfers-{period}.csv"'})

        @app.route("/api/search")
        def api_search():
            return jsonify(build_search_payload(request.args.get("q", "")))

        @app.route("/api/filelists")
        def api_filelists():
            offset, limit = parse_pagination_params(
                request.args.get("offset"), request.args.get("limit"))
            # ?list= names one of OUR served lists; absent means the primary.
            # ?q= narrows it to rows matching every word in it (#399's
            # follow-up) - absent or blank means unfiltered, same as before.
            return jsonify(build_filelists_payload(
                offset, limit, name=requested_own_list(request.args.get("list")),
                q=request.args.get("q", "")))

        # ------------------------------------------------------------------
        # These routes below DO mutate state (queuing an outbound IRC line,
        # dialling an IP:port a foreign bot supplies) - which is exactly why
        # they sit behind the same require_login() as everything else, same
        # as every other route in this app. See the module docstring.
        # ------------------------------------------------------------------

        @app.route("/api/search/broadcast", methods=["POST"])
        def api_search_broadcast():
            body = json_object(request.get_json(silent=True))
            status, result = start_broadcast_search(body.get("term", ""))
            return jsonify(result), status

        @app.route("/api/search/broadcast/status")
        def api_search_broadcast_status():
            return jsonify(build_broadcast_status_payload())

        @app.route("/api/fetch/enqueue", methods=["POST"])
        def api_fetch_enqueue():
            payload = request.get_json(silent=True)
            status, result = build_fetch_enqueue_result(payload)
            return jsonify(result), status

        @app.route("/api/fetch/status")
        def api_fetch_status():
            return jsonify(build_fetch_status_payload())

        @app.route("/api/fetch/paused")
        def api_fetch_paused():
            import dcc_fetch
            return jsonify(dcc_fetch.paused_bots())

        @app.route("/api/fetch/pause", methods=["POST"])
        def api_fetch_pause():
            status, result = build_fetch_pause_result(request.get_json(silent=True), True)
            return jsonify(result), status

        @app.route("/api/fetch/resume", methods=["POST"])
        def api_fetch_resume():
            status, result = build_fetch_pause_result(request.get_json(silent=True), False)
            return jsonify(result), status

        @app.route("/api/tools/verify-list")
        def api_tools_verify_list():
            return jsonify(build_verify_list_payload())

        @app.route("/api/tools/update-list", methods=["POST"])
        def api_tools_update_list():
            status, result = start_list_update()
            return jsonify(result), status

        @app.route("/api/tools/update-list/status")
        def api_tools_update_list_status():
            return jsonify(build_update_list_status_payload())

        # #1182: Read audio info. Its progress is in the status above.
        @app.route("/api/tools/audio-info", methods=["POST"])
        def api_tools_audio_info():
            status, result = start_audio_info_reading()
            return jsonify(result), status

        # #572: what the version check last found, and a check on request.
        @app.route("/api/version-check")
        def api_version_check():
            import version_check
            return jsonify(version_check.state())

        @app.route("/api/version-check", methods=["POST"])
        def api_version_check_now():
            import version_check
            return jsonify(version_check.manual_check())

        @app.route("/api/filelists/fetch", methods=["POST"])
        def api_filelists_fetch():
            body = json_object(request.get_json(silent=True))
            status, result = build_list_fetch_enqueue_result(body.get("bot", ""))
            return jsonify(result), status

        @app.route("/api/filelists/fetch-folder-rar", methods=["POST"])
        def api_filelists_fetch_folder_rar():
            payload = request.get_json(silent=True)
            if isinstance(payload, list):
                status, result = build_folder_rar_batch_enqueue_result(payload)
                return jsonify(result), status
            body = json_object(payload)
            status, result = build_folder_rar_fetch_enqueue_result(
                body.get("bot", ""), body.get("folder", ""), body.get("marker", ""), body.get("channel", ""))
            return jsonify(result), status

        @app.route("/api/settings/theme-preview", methods=["POST"])
        def api_theme_preview():
            # POST because it carries what the operator has typed. It changes
            # nothing - see build_theme_preview() - and sits behind the same
            # login as every other route here.
            return jsonify(build_theme_preview(
                theme_preview_overrides(request.get_json(silent=True))))

        @app.route("/api/filelists/bots")
        def api_filelists_bots():
            # EVERY source the List Browser can show: ours, then theirs.
            #
            # Ours first and in the operator's own order rather than
            # alphabetically - they arranged that order themselves, and the
            # primary being first is the one piece of it the page should not
            # reshuffle. Composed HERE rather than inside either builder,
            # because each of those answers a question its own name states and
            # neither should start answering the other's.
            return jsonify(build_own_list_summaries()
                           + build_fetched_bot_list_summaries())

        @app.route("/api/filelists/sources", methods=["POST"])
        def api_filelists_add_source():
            body = json_object(request.get_json(silent=True))
            status, result = build_add_source_result(body.get("bot", ""))
            return jsonify(result), status

        @app.route("/api/filelists/sources/<nick>/remove", methods=["POST"])
        def api_filelists_remove_source(nick):
            # POST .../remove rather than DELETE, the shape /api/fetch/<id>/
            # delete already uses: one verb everywhere the page mutates.
            status, result = build_remove_source_result(nick)
            return jsonify(result), status

        @app.route("/api/filelists/purge-offline", methods=["POST"])
        def api_filelists_purge_offline():
            # POST, not DELETE: it does not name what to remove - the caller
            # asks "clear whatever is offline right now" rather than naming a
            # specific bot, so there is no resource URL for DELETE to name.
            status, result = build_purge_offline_fetched_lists_result()
            return jsonify(result), status

        @app.route("/api/filelists/purge-all", methods=["POST"])
        def api_filelists_purge_all():
            # Same shape as purge-offline right above, deliberately a
            # separate route rather than a flag on it (#1260): one clears
            # what is already gone, the other clears everything regardless
            # of whether it answers right now - different enough blast
            # radius that a typo flipping one into the other should not be
            # possible.
            status, result = build_purge_all_fetched_lists_result()
            return jsonify(result), status

        @app.route("/api/filelists/search")
        def api_filelists_search():
            # Read-only, and it stays that way: this answers a question about
            # lists already on disk and queues nothing. Selecting a result and
            # queueing it goes through the existing POST /api/fetch/enqueue,
            # which is where the admission control for that already lives.
            _offset, limit = parse_pagination_params(
                None, request.args.get("limit"))
            return jsonify(build_crosslist_search_payload(
                request.args.get("q", ""), limit,
                online_only=request.args.get("online", "") in ("1", "true")))

        @app.route("/api/filelists/bot/<nick>")
        def api_filelists_bot(nick):
            offset, limit = parse_pagination_params(
                request.args.get("offset"), request.args.get("limit"))
            status, result = build_fetched_bot_list_payload(
                nick, offset, limit, list_marker=request.args.get("list", ""),
                q=request.args.get("q", ""))
            if status == 200:
                # Opened: no longer new (#926 item 6).
                import list_fetch
                list_fetch.mark_seen(nick)
            return jsonify(result), status

        @app.route("/api/fetch/<request_id>/download")
        def api_fetch_download(request_id):
            import dcc_fetch
            # Snapshot just the two fields needed, under the same lock the
            # writers use, so "complete"/stored_filename can never be read as
            # a torn pair - the lock is released before send_from_directory()
            # touches the filesystem.
            with dcc_fetch._fetch_lock():
                row = getattr(config, "fetch_queue", {}).get(request_id)
                if row and row.get("state") == "complete" and row.get("stored_filename"):
                    stored_filename = row["stored_filename"]
                    download_name = row.get("filename") or stored_filename
                else:
                    stored_filename = None
            if not stored_filename:
                return jsonify({"error": "Unknown, incomplete, or failed fetch."}), 404
            # Never build this path from the URL parameter - request_id only
            # selects a row, and the row's OWN already-validated
            # stored_filename (set by dcc_fetch.py after it ran the offer's
            # filename through dcc.is_safe_path()) is what actually gets
            # opened.
            directory = os.path.abspath(getattr(config, "FETCHED_FILES_DIR", "./data/fetched"))

            # NOT send_from_directory(), and the reason is specific.
            #
            # dcc_fetch fits a stored name to MAX_NAME_BYTES (255) and touches
            # every path through platform_compat.long_path(). This route was
            # the one that did not, so on Windows a fetch with a long
            # remote-chosen name was written, marked "complete", listed in the
            # UI - and its Download button answered 404 for ever, while the
            # file sat there the whole time. Found by audit.
            #
            # Handing send_from_directory() the WRAPPED directory does not fix
            # it either: werkzeug's safe_join() joins with a FORWARD SLASH, and
            # a \\?\ path is the one kind Windows will not accept those in. So
            # the path is built here, with a backslash, and wrapped after.
            #
            # The containment safe_join() was providing is kept explicitly:
            # dcc.is_safe_path() is the same gate every request path in the
            # project uses, and it resolves symlinks, which safe_join() does
            # not. `import dcc` is deferred exactly like `import dcc_fetch`
            # above - see tests/test_import_graph.py.
            import dcc

            candidate = os.path.join(directory, stored_filename)
            if not dcc.is_safe_path(directory, candidate):
                print(f"[WEB SECURITY] Refused a fetch download outside "
                      f"{directory}: {stored_filename!r}")
                return jsonify({"error": "Unknown, incomplete, or failed fetch."}), 404

            wrapped = platform_compat.long_path(candidate)
            if not os.path.isfile(wrapped):
                return jsonify({"error": "The fetched file is no longer on disk."}), 404

            return send_file(wrapped, as_attachment=True,
                             download_name=download_name)

        @app.route("/api/fetch/clear", methods=["POST"])
        def api_fetch_clear():
            status, result = build_fetch_clear_result(request.get_json(silent=True))
            return jsonify(result), status

        @app.route("/api/fetch/<request_id>/delete", methods=["POST"])
        def api_fetch_delete(request_id):
            # Cancel says {"only_waiting": true} (#1046): it means a request
            # that has not started, so a row that finished while the page was
            # stale or its confirm dialog open is refused, never its file removed.
            body = json_object(request.get_json(silent=True))
            only = ("pending", "offered", "queued") if body.get("only_waiting") is True else None
            status, result = build_fetch_delete_result(request_id, only_states=only)
            return jsonify(result), status

        @app.route("/api/settings")
        def api_settings():
            return jsonify(build_settings_payload())

        @app.route("/api/settings", methods=["POST"])
        def api_settings_save():
            body = json_object(request.get_json(silent=True))
            status, result = apply_settings_changes(body)
            return jsonify(result), status

        @app.route("/api/folders")
        def api_folders():
            return jsonify(build_folders_payload())

        # 404 rather than 403 when it is off, for the reason the console
        # routes give: 403 confirms the route exists and is merely disabled,
        # which tells anyone probing that this build has one worth returning
        # for.
        def _browser_is_available():
            return bool(getattr(config, "WEBUI_FOLDER_BROWSER_ENABLED", False))

        @app.route("/api/folders/browse")
        def api_folders_browse():
            if not _browser_is_available():
                return jsonify({"error": "not found"}), 404
            payload = build_folder_browse_payload(request.args.get("path", ""))
            return jsonify(payload), (400 if "error" in payload else 200)

        @app.route("/api/folders", methods=["POST"])
        def api_folders_save():
            body = json_object(request.get_json(silent=True))
            status, result = apply_folder_changes(body)
            return jsonify(result), status

        @app.route("/api/lists")
        def api_lists():
            return jsonify(build_lists_payload())

        @app.route("/api/lists", methods=["POST"])
        def api_lists_save():
            body = json_object(request.get_json(silent=True))
            status, result = apply_list_changes(body)
            return jsonify(result), status

        @app.route("/api/on-connect")
        def api_on_connect():
            return jsonify(build_on_connect_payload())

        @app.route("/api/tools/stop", methods=["POST"])
        def api_tools_stop():
            # The Tools page's Stop the bot (#1065): answered first, then the
            # same stop as Ctrl-C a moment later, so the page hears back.
            import stopping
            stopping.request_stop_soon("asked from the dashboard")
            return jsonify({"stopping": True}), 200

        @app.route("/api/on-connect/resend", methods=["POST"])
        def api_on_connect_resend():
            status, result = build_on_connect_resend_result()
            return jsonify(result), status

        @app.route("/api/on-connect", methods=["POST"])
        def api_on_connect_save():
            body = json_object(request.get_json(silent=True))
            status, result = apply_on_connect_changes(body)
            return jsonify(result), status

        @app.route("/api/settings/password", methods=["POST"])
        def api_settings_password():
            body = json_object(request.get_json(silent=True))
            status, result = build_password_change_result(
                body.get("new_password", ""), body.get("confirm_password", ""))
            if status == 200:
                restamp_session()
            return jsonify(result), status

        # 404, not 403, when the Console is off. 403 would confirm the routes
        # exist and are merely disabled, which tells anyone probing that this
        # build has an admin console worth coming back for. Not found is the
        # honest answer to "is there a console here" when there is not one
        # reachable, and the operator who wants it is looking at the setting,
        # not at the URL.
        #
        # Both routes check independently rather than sharing a decorator: they
        # are the whole attack surface of this feature, and a decorator applied
        # to one and forgotten on the other is a silent hole. Two lines each is
        # cheap enough to not be clever about.
        def _console_is_available():
            # ONE answer to "is the Console on", shared with the startup
            # notice and with anything else that asks. Reading the setting
            # directly here would have kept the old flat default alive on the
            # only paths that actually gate the feature - the routes - while
            # everything else moved.
            return console_is_enabled()

        @app.route("/api/filelists/<path:source>/purge", methods=["POST"])
        def api_filelists_purge(source):
            # POST, and <path:source> because a bot's other lists are named
            # "<nick>/<marker>" - a plain <source> stops at the slash and
            # would 404 on exactly those rows.
            status, result = build_fetched_list_purge_result(source)
            return jsonify(result), status

        @app.route("/api/messages")
        def api_messages():
            payload, status = messages_payload_or_404()
            return jsonify(payload), status

        @app.route("/api/messages/read", methods=["POST"])
        def api_messages_read():
            # POST because it changes what the operator has acknowledged.
            payload, status = messages_read_or_404()
            return jsonify(payload), status

        @app.route("/api/notices")
        def api_notices():
            return jsonify(build_notices_payload())

        @app.route("/api/notices/read", methods=["POST"])
        def api_notices_read():
            # POST because it changes what the operator has acknowledged.
            return jsonify(mark_notices_read_result())

        @app.route("/api/console/log")
        def api_console_log():
            if not _console_is_available():
                return jsonify({"error": "not found"}), 404
            return jsonify(build_console_log_payload(request.args.get("since")))

        @app.route("/api/console/command", methods=["POST"])
        def api_console_command():
            if not _console_is_available():
                return jsonify({"error": "not found"}), 404
            body = json_object(request.get_json(silent=True))
            status, result = build_console_command_result(
                body.get("command", ""), request.remote_addr)
            return jsonify(result), status

        return app


# This section is web_setup_page.py since #1322, a step of splitting this file. Imported back by
# name, so webserver.<name> still answers for each of them.
from web_setup_page import (  # noqa: F401
    SETUP_FIELDS,
    SETUP_LANGS,
    SETUP_PAGE,
    SETUP_SAVED_PAGE,
    _SETUP_HOSTS,
    _html,
    _setup_host_ok,
    _setup_strings,
    apply_setup,
    build_setup_fields,
    render_setup_page,
    render_setup_saved_page,
    validate_setup_form,
)


def setup_page_is_possible():
    """Can the daemon offer /setup instead of exiting on a blank config?
    Flask, and nothing else: the page is loopback and one-shot by design,
    so there is no setting that turns it off."""
    return HAVE_FLASK





















if HAVE_FLASK:

    def create_setup_app(token, on_done, port=8420):
        """The setup-only app: /setup, and nothing the real app has."""
        app = Flask("dccore-setup", static_folder=None)
        app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
        # `bound`: the browser the code was first presented from (#675,
        # audit L11). The link is handed to the OS opener, and on Linux
        # that is xdg-open with the URL in argv, which the browser keeps in
        # its own argv for as long as it runs - readable by any other local
        # user via ps, for the whole setup window, on a host where 127.0.0.1
        # is reachable by everyone. So the code is good for one browser:
        # the first request that carries it gets a cookie, and from then on
        # the code is accepted only together with that cookie. A second
        # browser with the code from ps is refused, and if it was somehow
        # first, the operator's own is - loudly, with what to do.
        state = {"done": False, "changes": None, "bound": None, "shadowed_by": None}
        SETUP_COOKIE = "dccore-setup"

        def refused(why):
            return (f"<!doctype html><meta charset='utf-8'><p style='font-family:sans-serif'>"
                    f"{_html(why)}</p>"), 403

        @app.before_request
        def gate():
            if not _setup_host_ok(request.headers.get("Host", "")):
                return refused("This page answers only to 127.0.0.1.")
            if state["done"]:
                return None
            supplied = request.args.get("token") or request.form.get("token") or ""
            import hmac
            if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
                return refused("Open the exact link printed in DCCore's window - it "
                               "carries a one-time code, so that only the person at "
                               "this machine can set the bot up.")
            if state["bound"] is None:
                import secrets
                state["bound"] = secrets.token_urlsafe(24)
                g.setup_cookie_to_set = state["bound"]
                return None
            held = request.cookies.get(SETUP_COOKIE, "")
            if not hmac.compare_digest(held.encode("utf-8"), state["bound"].encode("utf-8")):
                return refused("This link has already been opened in another browser, "
                               "and the code in it is good for one. If that was not "
                               "you, stop DCCore and start it again: it prints a new "
                               "link with a new code.")
            return None

        @app.after_request
        def bind_the_first_browser(response):
            value = getattr(g, "setup_cookie_to_set", None)
            if value:
                response.set_cookie(SETUP_COOKIE, value, httponly=True, samesite="Lax", path="/")
            return _refuse_framing(response)

        @app.route("/")
        def root():
            return redirect(f"/setup?token={token}")

        @app.route("/setup", methods=["GET", "POST"])
        def setup():
            lang = (request.values.get("lang") or "en").lower()
            lang = lang if lang in SETUP_LANGS else "en"
            if state["done"]:
                return render_setup_saved_page(state["changes"] or {}, lang, port,
                                               shadowed_by=state["shadowed_by"])
            if request.method == "GET":
                return render_setup_page(build_setup_fields(lang), token, lang, port=port)
            changes, password_hash, errors = validate_setup_form(request.form, lang)
            if errors:
                values = {k: v for k, v in request.form.items() if k not in ("password", "password_confirm", "token")}
                return render_setup_page(build_setup_fields(lang, values), token, lang,
                                         errors=errors, values=values, port=port), 400
            try:
                applied = apply_setup(changes, password_hash)
            except Exception as err:  # a full disk, a read-only folder
                message = _setup_strings(lang).get("setup.error.write", "Could not write the settings: {error}")
                return render_setup_page(build_setup_fields(lang, dict(request.form)), token, lang,
                                         errors=[("NICKNAME", message.replace("{error}", str(err)))],
                                         values=dict(request.form), port=port), 500
            state["done"] = True
            state["changes"] = changes
            state["shadowed_by"] = (applied or {}).get("shadowed_by")
            on_done(changes)
            return render_setup_saved_page(changes, lang, port, shadowed_by=state["shadowed_by"])

        @app.route("/login")
        def not_yet():
            # The saved page polls this; while the setup server still holds
            # the port the answer is "not yet", and once the real app has
            # it, its own login page answers with 200 and the poll redirects.
            return "", 503

        return app

    def run_setup_until_configured(host="127.0.0.1", port=None, log=print, opener=None,
                                   wait=None, token=None):
        """Serve /setup until the form has written the configuration; then
        stop and return what was written, so oserve.startup() can carry on.
        Returns None when the page could not be served at all (the port is
        taken), in which case the caller falls back to its old refusal."""
        import secrets
        from werkzeug.serving import make_server
        port = port or getattr(config, "WEBUI_PORT", 8420)
        token = token or secrets.token_urlsafe(24)
        done = threading.Event()
        result = {}

        def on_done(changes):
            result["changes"] = changes
            done.set()

        app = create_setup_app(token, on_done, port=port)
        try:
            server = make_server(host, port, app, threaded=True)
        except (OSError, SystemExit) as err:
            # SystemExit too: werkzeug's server calls sys.exit(1) on a bind
            # failure ("port in use", printed by it to stderr), and a taken
            # port must not take the daemon down - the caller falls back to
            # its refusal. That SystemExit printed as "(1)" here, and the
            # only causes and the only ways out went unsaid (#617).
            reason = "the port is taken" if isinstance(err, SystemExit) else str(err)
            log(f"[SETUP] Could not open the setup page on {host}:{port} - {reason}.")
            log("[SETUP] Is another DCCore still running, maybe in a minimised window? "
                "Stop it, or free the port, or put another WEBUI_PORT in settings.conf - "
                "or answer the questions in the terminal instead: python3 configure.py")
            return None
        thread = threading.Thread(target=server.serve_forever, name="dccore-setup", daemon=True)
        thread.start()
        url = f"http://{host}:{port}/setup?token={token}"
        log("[SETUP] No configuration yet. Set DCCore up in your browser:")
        log(f"[SETUP]     {url}")
        log("[SETUP] (The code in the link is what lets only you use this page. "
            "The bot starts as soon as the form is saved.)")
        _quiet_the_request_log()
        opened = False
        if getattr(config, "WEBUI_OPEN_BROWSER", True):
            try:
                import webbrowser
                opened = bool((opener or webbrowser.open)(url))
            except Exception as err:
                log(f"[SETUP] Could not open a browser ({err}); open the link above yourself.")
        # A page on this machine's 127.0.0.1 is no use to someone who is not at
        # it (#595). webbrowser.open() says False on a machine with no browser
        # and this used to ignore that, then wait for ever with nothing on
        # screen saying how else to go on.
        if not opened:
            log("[SETUP] No browser was opened here. If this machine is one you reach over "
                "SSH, tunnel the port (ssh -L "
                f"{port}:127.0.0.1:{port} <this machine>) and open the link on your own "
                "computer.")
        log("[SETUP] To answer the questions in this window instead, press Ctrl-C and run: "
            "python3 configure.py")
        try:
            # A loop rather than one wait(): a Windows console cannot deliver
            # Ctrl-C into an indefinite Event.wait(). `wait`, for tests, is
            # asked between waits whether to give up.
            give_up = wait or (lambda: False)
            while not done.wait(0.5):
                if give_up():
                    break
        except KeyboardInterrupt:
            # Ctrl-C is how somebody who cannot reach the page leaves; it used to
            # end in a traceback with nothing about what to do next (#595).
            log("[SETUP] Stopped. Answer the questions here instead: python3 configure.py")
        finally:
            # A moment for the "Saved" page to reach the browser before the
            # socket goes away; then the port is free for the real dashboard.
            if done.is_set():
                time.sleep(0.5)
            server.shutdown()
            server.server_close()
        if not done.is_set():
            return None
        # The saved page in the browser polls /login and goes there when the
        # dashboard answers (#689) - exactly when the dashboard was chosen
        # and a browser was opened here. start() must not open a second tab.
        global _browser_is_on_the_saved_page
        _browser_is_on_the_saved_page = bool(opened and result["changes"].get("WEBUI_ENABLED"))
        log(f"[SETUP] Settings written: {', '.join(result['changes'])}. Starting.")
        return result["changes"]


def start():
    """Run the dashboard. Called on its own daemon thread from oserve.startup().

    Logs and returns rather than raising on any of: Flask missing, the feature
    disabled via config, or the port being unavailable - none of those may take
    the daemon down with them.
    """
    if not HAVE_FLASK:
        print("[WEBUI] Flask not installed; dashboard disabled.")
        return
    # See oserve.startup(): absent means off, the same way config.py ships it.
    if not getattr(config, "WEBUI_ENABLED", False):
        print("[WEBUI] Disabled via config.WEBUI_ENABLED = False.")
        return
    if not adminchat.password_is_configured():
        print("[WEBUI] ADMIN_PASSWORD_HASH is not set; refusing to start the dashboard "
              "without a login. Generate one with `python src/adminchat.py` and put the "
              "result in admin_config.py or settings.conf.")
        return

    # 127.0.0.1 when absent, matching config.py. 0.0.0.0 would bind every
    # interface and put the dashboard on the LAN, which is the opposite of
    # what a missing setting should buy anyone - even with a login gate, that
    # is a decision the operator should make explicitly, not by omission.
    host = getattr(config, "WEBUI_HOST", "127.0.0.1")
    port = getattr(config, "WEBUI_PORT", 8420)
    _ensure_console_sink()
    app = create_app()
    print(f"[WEBUI] Dashboard starting on http://{host}:{port}/ (login required).")
    if console_is_enabled():
        print("[WEBUI] The Console is on. It reaches ban, rehash and update "
              "behind this one password - see WEBUI_CONSOLE_ENABLED.")
    _quiet_the_request_log()
    _open_in_browser(host, port)
    try:
        # use_reloader=False is NOT optional: Flask's reloader re-execs the whole
        # process, and this runs on an already-live daemon thread - a re-exec
        # here would take the entire bot down with it, not just the dashboard.
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as run_err:
        print(f"[WEBUI] Dashboard stopped: {run_err}")
